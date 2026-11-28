"""Advisory SessionStart hook — say when this clone is not wired for the graph.

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`,
which git never clones. So a repo that committed the checkers and the
`.gitattributes` still starts every fresh clone with the hooks inert and the
graph diffing as "Binary files differ", with no error anywhere to say so. This
hook is the one thing that notices.

**It never wires a clone.** Git leaves `.git/config` out of a clone on purpose:
cloning a repo must not be able to make it execute code. A plugin that wired
`core.hooksPath` on the user's behalf would route around that protection and
make the repo's vendored `.githooks/*.py` live in a fresh clone without anyone
choosing to run them. A plugin the user installed may suggest; it may not decide.

It makes two writes, both to wiring someone already installed, and prints one
line for each:
  - a `diff.codegraph.textconv` that pins a codebase-kg release older than this
    plugin moves to this plugin's release, once that tag is published (`repin`)
  - run with `--codex`, as `codex-hooks.json` does, the codebase-kg paths in
    Codex's `config.toml` move to this plugin's folder (`_codex_config.py`)

It also prints the platform hint (`platform_hint`): one line that names WSL 2 on
Windows outside WSL, once for the machine, and one line in WSL when the project
is on a Windows drive, once for the project. Its only write is the record of
which hints were shown, in the plugin's cache folder.

`SKIP_KG` skips all of it, the Codex config refresh included.

The notice is silent unless every one of these holds:
  - inside a git work tree
  - the graph (`graph_path`, default `knowledge/code_graph.db`) exists
  - a hooks dir with the vendored checkers exists
  - `core.hooksPath` is unset, OR `diff.codegraph.textconv` is unset, OR the
    textconv names a codebase-kg path that no longer exists
and never when `core.hooksPath` already points somewhere else — that repo made a
deliberate choice and does not need nagging toward clobbering its own config.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _codex_config import refresh as refresh_codex_config
from _config import (
    as_local_path,
    find_graph,
    graph_meta,
    load_config,
    project_dir,
    read_state,
    skip_kg,
    write_state,
)
from _shim_loader import load_shim

DEFAULT_HOOKS_DIR = ".githooks"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
# hooks.json gives this hook 10 s. Every git call below shares one deadline a little
# inside that, so a stale pin on an offline machine cannot push the hook past it.
HOOK_BUDGET = 8.0
GIT_TIMEOUT = 3
REPIN_TIMEOUT = 4
_deadline = float("inf")  # set by main()


def _timeout(cap: float) -> float | None:
    """Seconds the next git call may take: `cap`, cut to what is left of the budget.
    None when the budget is spent, and the call is skipped."""
    left = _deadline - time.monotonic()
    return min(cap, left) if left > 0.2 else None


# The first release that reads each graph schema. A driver pinned below it cannot
# export a graph at that schema, and `git diff` on the graph fails. Add a row with
# every schema bump (docs/RELEASING.md); test_session_start.py checks the newest
# schema has one.
SCHEMA_SINCE = {4: "0.7.0"}
_PINNED_TAG = re.compile(r"codebase-kg--v(\d+(?:\.\d+)*)")
_PIN_SOURCE = re.compile(
    r"git\+(?P<url>[^\s\"'#]+?)@codebase-kg--v(?P<version>\d+(?:\.\d+)*)"
)
_PLUGIN_PATH = re.compile(r"(?<![\w:/.~}\-])(?:[A-Za-z]:)?[/\\][^'\"\s;,)]*codebase-kg[^'\"\s;,)]*")
# Both must be present: kg_pre_commit.py imports the coverage rules from
# kg_pre_push.py beside it, so one without the other is not a wired repo.
VENDORED = ("kg_pre_push.py", "kg_pre_commit.py")

PLATFORM_HINT_FILE = "platform-hint.json"
PLATFORM_HINT_ENV = "CODEBASE_KG_PLATFORM_HINT"
PROC_MOUNTS = Path("/proc/mounts")
# WSL 2 mounts a Windows drive as 9p, WSL 1 as drvfs.
WINDOWS_MOUNT_TYPES = {"9p", "drvfs"}
WSL_GUIDE = "https://github.com/Alexk413x/codebase-kg#windows-use-wsl-2"


def _git(proj: Path, *args: str) -> str:
    """A read-only git query, or "" for anything that goes wrong.

    Every caller here reads. `git config --get` exits 1 for an unset key, which is
    a normal answer, not an error. The only writes are `repin`'s and the Codex
    config refresh.
    """
    limit = _timeout(GIT_TIMEOUT)
    if limit is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(proj), *args],
            capture_output=True,
            text=True,
            timeout=limit,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _hooks_dir(proj: Path, configured: str) -> Path | None:
    """The dir holding the vendored checkers, or None if this repo has none.

    A configured `core.hooksPath` wins even when it holds no checkers: that is
    exactly the case this hook must stay quiet about.
    """
    cand = proj / configured if configured else proj / DEFAULT_HOOKS_DIR
    if not cand.is_dir():
        return None
    return cand if all((cand / name).is_file() for name in VENDORED) else None


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def plugin_version() -> str:
    try:
        return str(json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def serves_installs() -> bool:
    """True for a release build or a copy a client installed. A checkout or a scratch copy is neither,
    and its version is not one a clone's diff driver should follow."""
    try:
        shim = load_shim()
        return bool(shim.released() or shim.installed())
    except Exception:  # noqa: BLE001
        return False


def stale_pin(textconv: str, running: str) -> str | None:
    """Why this textconv is out of date, or None when it is current or not ours."""
    if "codebase-kg" not in textconv:
        return None
    tag = _PINNED_TAG.search(textconv)
    if tag:
        if running and _version(tag.group(1)) < _version(running):
            return f"pins codebase-kg {tag.group(1)}, older than the installed {running}"
        return None
    for raw in _PLUGIN_PATH.findall(textconv):
        if not as_local_path(raw).exists():
            return f"names {raw}, which no longer exists"
    return None


def tag_published(url: str, version: str) -> bool:
    """True when `url` lists the release tag for `version`. One network read, no prompt."""
    tag = f"refs/tags/codebase-kg--v{version}"
    limit = _timeout(REPIN_TIMEOUT)
    if limit is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--tags", "--", url, tag],
            capture_output=True,
            text=True,
            timeout=limit,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and any(line.endswith(tag) for line in proc.stdout.splitlines())


def repin(proj: Path, textconv: str, running: str) -> str | None:
    """Move a tag-pinned textconv to the installed release. Returns the line to print, or None.

    One of the two writes this hook makes; the other is the Codex config refresh.
    It changes the release of the exporter in a driver someone already installed,
    and only once the new tag is published: a driver that names a missing tag
    makes `git diff` on the graph fail.
    """
    source = _PIN_SOURCE.search(textconv)
    if not source or not running:
        return None
    old = source.group("version")
    if _version(old) >= _version(running) or not tag_published(source.group("url"), running):
        return None
    moved = textconv[: source.start("version")] + running + textconv[source.end("version") :]
    limit = _timeout(GIT_TIMEOUT)
    if limit is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(proj), "config", "diff.codegraph.textconv", moved],
            capture_output=True,
            timeout=limit,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (
        f"codebase-kg: moved this clone's graph diff driver from codebase-kg {old} to "
        f"{running}, the installed plugin."
    )


def unreadable_pin(graph: Path, pinned: str) -> str | None:
    """Why a driver pinned to `pinned` cannot export `graph`, or None when it can."""
    try:
        schema = int(graph_meta(graph, "schema_version") or 0)
    except ValueError:
        return None
    needed = SCHEMA_SINCE.get(schema)
    if needed and _version(pinned) < _version(needed):
        return f"schema v{schema} needs codebase-kg {needed} or later"
    return None


def advice(proj: Path) -> str | None:
    """The one line to print, or None to stay silent."""
    cfg = load_config(proj)
    graph = find_graph(proj, cfg)
    if graph is None:
        return None
    if _git(proj, "rev-parse", "--is-inside-work-tree") != "true":
        return None

    configured = _git(proj, "config", "--get", "core.hooksPath")
    hooks = _hooks_dir(proj, configured)
    if hooks is None:
        return None

    textconv = _git(proj, "config", "--get", "diff.codegraph.textconv")
    if configured and textconv:
        running = plugin_version()
        why = stale_pin(textconv, running)
        if why is None:
            return None  # wired, and the driver is current
        if _PINNED_TAG.search(textconv):
            pinned = _PINNED_TAG.search(textconv).group(1)  # type: ignore[union-attr]
            if not serves_installs():
                broken = unreadable_pin(graph, pinned)
                if broken is None:
                    return None
                return (
                    f"codebase-kg: this clone's graph diff driver pins codebase-kg {pinned}, which "
                    f"cannot read {graph.name} ({broken}). git diff on the graph fails until the "
                    "pin moves. Run /codebase-kg:setup to update it."
                )
            moved = repin(proj, textconv, running)
            if moved:
                return moved
            # The new tag could not be confirmed (offline, unpublished). An older
            # pin that still exports the graph needs no word; the next session
            # tries again. One that cannot read the graph breaks `git diff` now.
            broken = unreadable_pin(graph, pinned)
            if broken is None:
                return None
            return (
                f"codebase-kg: this clone's graph diff driver pins codebase-kg {pinned}, which "
                f"cannot read {graph.name} ({broken}), and the pin could not be moved to the "
                f"installed {running}: its release tag was not found. git diff on the graph "
                "fails until the pin moves. The next session tries again; when you are online, "
                "/codebase-kg:setup also updates it."
            )
        return (
            f"codebase-kg: this clone's graph diff driver {why}, so git diff on "
            f"{graph.name} may fail or show an old format. Run /codebase-kg:setup to update it."
        )

    try:
        where = hooks.relative_to(proj).as_posix()
    except ValueError:
        where = str(hooks)
    missing = "git hooks and graph diffs" if not configured else "graph diffs"
    fix = f"sh {where}/install.sh" if (hooks / "install.sh").is_file() else "/codebase-kg:setup"
    return (
        f"codebase-kg: this clone is not wired for {graph.name} ({missing}). "
        f"git does not clone .git/config, so run:  {fix}"
    )


def on_windows() -> bool:
    return sys.platform == "win32"


def in_wsl() -> bool:
    return sys.platform == "linux" and (
        bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()
    )


def windows_mount(proj: Path, mounts: Path = PROC_MOUNTS) -> str | None:
    """The mount point of the Windows drive that holds `proj`, or None when a Linux file system holds it."""
    try:
        lines = mounts.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = proj.as_posix()
    found, fstype = "", ""
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = fields[1].replace("\\040", " ")
        inside = target == mount or target.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > len(found):
            found, fstype = mount, fields[2]
    return found if fstype in WINDOWS_MOUNT_TYPES else None


def _hint_once(key: str, text: str) -> str | None:
    """`text` the first time `key` is seen on this machine, then None. None too when the
    record cannot be written, so a read-only cache folder does not repeat the hint every session."""
    try:
        record = Path(load_shim().cache_dir()) / PLATFORM_HINT_FILE
    except Exception:  # noqa: BLE001
        return None
    seen = read_state(record)
    if key in seen:
        return None
    seen[key] = True
    return text if write_state(record, seen) else None


def platform_hint(proj: Path) -> str | None:
    """One line about where the project runs, at most once, or None.

    On Windows outside WSL it names WSL 2, once for the machine. In WSL with the project on a
    Windows drive it names the Linux home folder, once for the project. Silent in a repo with no
    graph, when `platform_hint` is off in the per-clone file, and when the environment variable
    `CODEBASE_KG_PLATFORM_HINT` is `off`.
    """
    if os.environ.get(PLATFORM_HINT_ENV, "").strip().lower() in {"off", "0", "false", "no"}:
        return None
    cfg = load_config(proj)
    if cfg.get("platform_hint") is False or find_graph(proj, cfg) is None:
        return None
    if on_windows():
        return _hint_once(
            "windows",
            "codebase-kg: on Windows, Git and file operations run faster when the project and "
            f"the agent both run inside WSL 2. Setup: {WSL_GUIDE}  This notice shows once.",
        )
    if in_wsl():
        mount = windows_mount(proj)
        if mount:
            return _hint_once(
                f"wsl:{proj.as_posix()}",
                f"codebase-kg: this project is on a Windows drive ({mount}) and the agent runs in "
                "WSL, so every file operation crosses between the two systems. That is slower "
                "than either system alone. Clone the project under your Linux home folder (~). "
                f"Setup: {WSL_GUIDE}  This notice shows once for this project.",
            )
    return None


def main() -> None:
    global _deadline
    _deadline = time.monotonic() + HOOK_BUDGET
    try:
        if skip_kg():
            return
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return
        cwd = data.get("cwd")
        proj = project_dir(cwd if isinstance(cwd, str) else None)
        messages = [advice(proj), platform_hint(proj)]
        if "--codex" in sys.argv[1:]:
            messages.append(refresh_codex_config(PLUGIN_ROOT))
        text = "\n".join(m for m in messages if m)
        if text:
            print(json.dumps({"systemMessage": text}))
    except Exception:  # noqa: BLE001
        # Advisory. A session must never fail to start because of this.
        return
    finally:
        _deadline = float("inf")


if __name__ == "__main__":
    main()

"""Advisory SessionStart hook — say when this clone is not wired for the graph.

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`,
which git never clones. So a repo that committed the checkers and the
`.gitattributes` still starts every fresh clone with the hooks inert and the
graph diffing as "Binary files differ", with no error anywhere to say so. This
hook is the one thing that notices.

**It prints one line and does nothing else.** It never runs `git config` for
write, never touches a file. Git leaves `.git/config` out of a clone on purpose:
cloning a repo must not be able to make it execute code. A plugin that wired
`core.hooksPath` on the user's behalf would route around that protection and
make the repo's vendored `.githooks/*.py` live in a fresh clone without anyone
choosing to run them. A plugin the user installed may suggest; it may not decide.

Silent unless every one of these holds:
  - inside a git work tree
  - the graph (`graph_path`, default `knowledge/code_graph.db`) exists
  - a hooks dir with the vendored checkers exists
  - `core.hooksPath` is unset, OR `diff.codegraph.textconv` is unset
and never when `core.hooksPath` already points somewhere else — that repo made a
deliberate choice and does not need nagging toward clobbering its own config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import find_graph, load_config, project_dir  # noqa: E402

DEFAULT_HOOKS_DIR = ".githooks"
# Both must be present: kg_pre_commit.py imports the coverage rules from
# kg_pre_push.py beside it, so one without the other is not a wired repo.
VENDORED = ("kg_pre_push.py", "kg_pre_commit.py")


def _git(proj: Path, *args: str) -> str:
    """A read-only git query, or "" for anything that goes wrong.

    Every caller here reads; nothing in this hook writes. `git config --get`
    exits 1 for an unset key, which is a normal answer, not an error.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(proj), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return False


def _hooks_dir(proj: Path, configured: str) -> Path | None:
    """The dir holding the vendored checkers, or None if this repo has none.

    A configured `core.hooksPath` wins even when it holds no checkers: that is
    exactly the case this hook must stay quiet about.
    """
    cand = proj / configured if configured else proj / DEFAULT_HOOKS_DIR
    if not cand.is_dir():
        return None
    return cand if all((cand / name).is_file() for name in VENDORED) else None


def advice(proj: Path) -> str | None:
    """The one line to print, or None to stay silent."""
    if _git(proj, "rev-parse", "--is-inside-work-tree") != "true":
        return None

    cfg = load_config(proj)
    graph = find_graph(proj, cfg)
    if graph is None:
        return None

    configured = _git(proj, "config", "--get", "core.hooksPath")
    hooks = _hooks_dir(proj, configured)
    if hooks is None:
        return None
    if configured and not _same_dir(proj / configured, hooks):
        return None  # this repo points its hooks somewhere else on purpose

    textconv = _git(proj, "config", "--get", "diff.codegraph.textconv")
    if configured and textconv:
        return None  # already wired

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


def main() -> None:
    try:
        if os.environ.get("SKIP_KG"):
            return
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return
        cwd = data.get("cwd")
        message = advice(project_dir(cwd if isinstance(cwd, str) else None))
        if message:
            print(json.dumps({"systemMessage": message}))
    except Exception:
        # Advisory. A session must never fail to start because of this.
        return


if __name__ == "__main__":
    main()

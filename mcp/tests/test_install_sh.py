"""`git-hooks/install.sh` — the per-clone wiring, tested from a fresh clone.

The defect this file pins: `core.hooksPath` and the `diff.codegraph.*` settings
live in `.git/config`, which git never clones. Wiring them in the shell that ran
setup leaves every other checkout with inert hooks and "Binary files differ",
and nothing anywhere reports it. So these tests never assert against the shell
that did the wiring — they clone, run the one command, and ask git what it
prints.

The textconv driver is exercised through `KG_TEXTCONV` pointed at this
checkout's exporter. The shipped default is a tag-pinned `uvx --from git+…` URL,
which needs the network and credentials for a private repo; the wiring is what
is under test here, not uv's resolver.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parent.parent.parent
GIT_HOOKS = ROOT / "git-hooks"
SRC = ROOT / "mcp" / "src"
VENDORED = ("kg_pre_commit.py", "kg_pre_push.py", "pre-commit", "pre-push", "install.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="needs git and a POSIX sh",
)

# A local stand-in for the pinned uvx URL: same contract (one path in, JSON out),
# no network and no credentials.
LOCAL_TEXTCONV = f'"{sys.executable}" -m codebase_kg.export'


def _env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
        "KG_TEXTCONV": LOCAL_TEXTCONV,
    }


def _run(cwd: Path, *args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False,
        env=_env(home or cwd),
    )


def _git(repo: Path, *args: str, home: Path | None = None) -> str:
    proc = _run(repo, "git", *args, home=home)
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stderr}"
    return proc.stdout.strip()


def _graph(path: Path, description: str, generated: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    build(
        path,
        Meta(codebase="t", root="src", generated=generated),
        [Node(id="a", kind="Domain", description=description,
              anchors=[Anchor("A.py", "A")], edges=[], section="S")],
    )


@pytest.fixture
def wired_repo(tmp_path: Path) -> Path:
    """A repo wired the way /codebase-kg:setup leaves one: checkers, installer,
    `.gitattributes` and the exec bits all committed. Nothing in `.git/config` —
    that is the half a clone never receives."""
    repo = tmp_path / "origin"
    (repo / ".githooks").mkdir(parents=True)
    (repo / "src").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for name in VENDORED:
        shutil.copy(GIT_HOOKS / name, repo / ".githooks" / name)
    (repo / ".gitattributes").write_text(
        "knowledge/code_graph.db binary diff=codegraph\n", encoding="utf-8"
    )
    (repo / "src" / "A.py").write_text("class A: pass\n", encoding="utf-8")
    _graph(repo / "knowledge" / "code_graph.db", "First thing.", "2026-08-01")
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x",
         ".githooks/pre-commit", ".githooks/pre-push",
         ".githooks/kg_pre_commit.py", ".githooks/kg_pre_push.py")
    _git(repo, "commit", "-qm", "one")
    _graph(repo / "knowledge" / "code_graph.db", "Second thing, renamed.", "2026-08-02")
    _git(repo, "commit", "-qam", "two")
    return repo


@pytest.fixture
def clone(wired_repo: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "clone"
    _git(wired_repo, "clone", "-q", str(wired_repo), str(dest))
    # cachetextconv writes its cache to a notes ref, which needs an identity.
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "t")
    return dest


def _diff(clone: Path) -> str:
    return _git(clone, "diff", "HEAD~1", "HEAD", "--", "knowledge/code_graph.db")


def test_a_clone_starts_unwired(clone: Path) -> None:
    """The premise. Without this, none of the rest means anything."""
    assert "Binary files" in _diff(clone)
    assert _run(clone, "git", "config", "--get", "core.hooksPath").stdout.strip() == ""


def test_one_command_wires_a_fresh_clone(clone: Path) -> None:
    proc = _run(clone, "sh", ".githooks/install.sh")
    assert proc.returncode == 0, proc.stderr
    assert _git(clone, "config", "--get", "core.hooksPath") == ".githooks"
    diff = _diff(clone)
    assert "Binary files" not in diff
    assert '-      "description": "First thing."' in diff
    assert '+      "description": "Second thing, renamed."' in diff


def test_running_it_twice_changes_nothing(clone: Path) -> None:
    _run(clone, "sh", ".githooks/install.sh")
    after_first = (clone / ".git" / "config").read_text(encoding="utf-8")
    second = _run(clone, "sh", ".githooks/install.sh")
    assert second.returncode == 0, second.stderr
    assert (clone / ".git" / "config").read_text(encoding="utf-8") == after_first
    assert "already" in second.stdout


def test_the_hooks_actually_fire(clone: Path) -> None:
    _run(clone, "sh", ".githooks/install.sh")
    (clone / "src" / "B.py").write_text("class B: pass\n", encoding="utf-8")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    _git(clone, "add", "src/B.py")
    proc = _run(clone, "git", "commit", "-m", "three")
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "[codebase-kg]" in combined and "src/B.py" in combined


def test_it_refuses_to_clobber_a_hooks_path_pointing_elsewhere(clone: Path) -> None:
    """Someone else's deliberate choice. Refuse loudly rather than reroute it."""
    (clone / "other-hooks").mkdir()
    _git(clone, "config", "core.hooksPath", "other-hooks")
    proc = _run(clone, "sh", ".githooks/install.sh")
    assert proc.returncode != 0
    assert _git(clone, "config", "--get", "core.hooksPath") == "other-hooks"
    assert "will not" in proc.stderr


def test_it_names_hooks_committed_non_executable(tmp_path: Path, wired_repo: Path) -> None:
    """Git skips a non-executable hook on macOS and Linux with no message at all,
    so a 100644 in the index is a silently dead check."""
    _git(wired_repo, "update-index", "--chmod=-x", ".githooks/pre-commit")
    _git(wired_repo, "commit", "-qam", "unexec")
    dest = tmp_path / "clone2"
    _git(wired_repo, "clone", "-q", str(wired_repo), str(dest))
    proc = _run(dest, "sh", ".githooks/install.sh")
    assert "update-index --chmod=+x" in proc.stderr
    assert ".githooks/pre-commit" in proc.stderr


def test_it_warns_when_the_gitattributes_routing_is_missing(
    tmp_path: Path, wired_repo: Path
) -> None:
    """textconv does nothing without the attribute that routes the file to it."""
    (wired_repo / ".gitattributes").write_text("", encoding="utf-8")
    _git(wired_repo, "commit", "-qam", "drop attrs")
    dest = tmp_path / "clone3"
    _git(wired_repo, "clone", "-q", str(wired_repo), str(dest))
    proc = _run(dest, "sh", ".githooks/install.sh")
    assert "diff=codegraph" in proc.stderr


def test_the_shipped_default_is_a_pinned_remote_not_a_local_path() -> None:
    """The defect in the old setup: `${CLAUDE_PLUGIN_ROOT}` expands to a
    version-stamped local cache path, gets written verbatim into .git/config,
    and resolves on exactly one machine — until the next plugin update."""
    src = (GIT_HOOKS / "install.sh").read_text(encoding="utf-8")
    assert "CLAUDE_PLUGIN_ROOT" not in src
    assert "git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v" in src
    assert "#subdirectory=mcp" in src
    # Without --quiet, uv prints resolution lines into the body of every diff.
    assert "uvx --quiet --from" in src


def test_the_pin_tracks_the_plugin_version() -> None:
    """Setup stamps this line; the default must be the plugin's own version, so a
    repo wired by this checkout pins the tag this checkout ships."""
    version = json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    src = (GIT_HOOKS / "install.sh").read_text(encoding="utf-8")
    assert f'KG_VERSION="${{KG_VERSION:-{version}}}"' in src


def test_a_broken_driver_is_not_left_configured(clone: Path) -> None:
    """A configured textconv that fails does not degrade to "Binary files
    differ" — git aborts with "unable to read files to diff". An unreachable
    driver is worse than no driver, so it is probed before it is written."""
    env = _env(clone)
    env["KG_TEXTCONV"] = f'"{sys.executable}" -c "raise SystemExit(1)"'
    proc = subprocess.run(
        ["sh", ".githooks/install.sh"], cwd=clone, capture_output=True,
        text=True, check=False, env=env,
    )
    assert proc.returncode != 0
    assert _run(clone, "git", "config", "--get", "diff.codegraph.textconv").stdout.strip() == ""
    assert "Binary files" in _diff(clone)


def test_a_later_run_clears_a_driver_that_stopped_working(clone: Path) -> None:
    """The tag moves, access is revoked, uv's cache is wiped — whatever the
    cause, re-running must leave the repo diffable rather than fatal."""
    assert _run(clone, "sh", ".githooks/install.sh").returncode == 0
    assert _git(clone, "config", "--get", "diff.codegraph.textconv") == LOCAL_TEXTCONV
    env = _env(clone)
    env["KG_TEXTCONV"] = f'"{sys.executable}" -c "raise SystemExit(1)"'
    subprocess.run(["sh", ".githooks/install.sh"], cwd=clone, capture_output=True,
                   text=True, check=False, env=env)
    assert _run(clone, "git", "config", "--get", "diff.codegraph.textconv").stdout.strip() == ""
    assert "Binary files" in _diff(clone)


def test_the_shipped_templates_are_committed_executable() -> None:
    """`cp` preserves the source mode. A template committed 100644 here becomes a
    hook committed 100644 in every repo setup wires — and git skips a
    non-executable hook on macOS and Linux with no message at all."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", "git-hooks/"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout
    modes = {
        line.split("\t")[1]: line.split()[0]
        for line in listing.strip().splitlines() if line
    }
    for name in ("pre-commit", "pre-push", "kg_pre_commit.py", "kg_pre_push.py",
                 "install.sh"):
        assert modes.get(f"git-hooks/{name}") == "100755", f"{name} is {modes.get(f'git-hooks/{name}')}"

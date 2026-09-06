"""The status-`M` drift check, driven by real git.

`analyze` had unit tests and still missed the drift that matters. It split a
change set into `A` (unmapped) and `D` (still anchored) and dropped `M` on the
floor, while the `source` table of digests — written on every build, described in
SCHEMA.md §6.3 as the thing that makes a green `kg_validate` mean something —
was read by neither hook. On one repo that let a graph fall 48 commits behind:
the check would have named the 27 new files and said nothing about the 47
modified ones, which were two thirds of the drift.

The pure-function half lives in `test_pre_push.py`. These tests exercise the
half that talks to git, because that is where this can be quietly wrong: the
digest has to be read out of the index or the pushed commit (never the working
tree), it has to be SHA-256 over raw bytes so it compares equal to what
`writer.file_sha` recorded, and `git cat-file --batch` output has to be parsed
without desynchronising on a missing object.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"
sys.path.insert(0, str(HOOKS))

import kg_pre_commit as commit_hook  # noqa: E402
import kg_pre_push as g  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ORIGINAL = b"class Known { fun rank() = 1 }\n"
REWRITTEN = b"class Known { fun rank() = weightedByRecency() }\n"


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)}
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, env=env, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stderr.decode()}"
    return proc.stdout.decode("utf-8", "replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one mapped file, committed, and a graph baselined against it."""
    repo = tmp_path / "repo"
    (repo / "src" / "ui").mkdir(parents=True)
    (repo / "src" / "ui" / "Known.kt").write_bytes(ORIGINAL)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    db = repo / "knowledge" / "code_graph.db"
    db.parent.mkdir()
    build(
        db,
        Meta(codebase="test", root="src", generated="2026-08-01"),
        [Node(
            id="known", kind="Ui", description="Ranks the feed.",
            anchors=[Anchor("ui/Known.kt", "Known")], edges=[], section="UI",
        )],
        source_root=repo / "src",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _run_commit_hook(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The pre-commit hook against `repo`, returning what it wrote to stderr."""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("SKIP_KG", raising=False)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "kg_pre_commit.py")],
        cwd=repo, capture_output=True, check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo), "SKIP_KG": ""},
    )
    assert proc.returncode == 0, "the check is advisory and must always exit 0"
    return proc.stderr.decode("utf-8", "replace")


# --- the gap this closes -----------------------------------------------------
def test_a_staged_rewrite_of_a_mapped_file_is_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the hook was silent on. The anchor still resolves — `Known` is
    still there — so nothing else in the toolchain notices."""
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "src/ui/Known.kt")
    err = _run_commit_hook(repo, monkeypatch)
    assert "src/ui/Known.kt" in err
    assert "changed since" in err


def test_an_unchanged_repo_stays_silent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_commit_hook(repo, monkeypatch) == ""


def test_touching_a_mapped_file_without_changing_it_is_silent(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged but byte-identical — a revert, or a commit that only reformatted
    something else. A digest comparison must not fire on the mere fact of a
    file appearing in the change set."""
    (repo / "src" / "ui" / "Known.kt").write_bytes(ORIGINAL)
    _git(repo, "add", "-A")
    assert _run_commit_hook(repo, monkeypatch) == ""


def test_the_digest_comes_from_the_index_not_the_working_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staged bytes are what the commit will contain. An unstaged edit on
    top of them must not be reported — the hook would be describing a commit
    nobody is making."""
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "src/ui/Known.kt")
    _git(repo, "stash", "-q")  # index and tree both back to ORIGINAL
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)  # working tree only
    assert _run_commit_hook(repo, monkeypatch) == ""


# --- the git plumbing --------------------------------------------------------
def test_blob_digests_matches_the_recorded_baseline(repo: Path) -> None:
    """The comparison is only meaningful if both sides hash identically —
    SHA-256 over raw bytes, the way `writer.file_sha` does it. A different
    normalisation reads as permanent drift on every file."""
    monkeypatch_cwd = os.getcwd()
    try:
        os.chdir(repo)
        got = g.blob_digests(["HEAD:src/ui/Known.kt"])
    finally:
        os.chdir(monkeypatch_cwd)
    graph = g.read_graph(repo / "knowledge" / "code_graph.db")
    assert graph is not None
    assert got["HEAD:src/ui/Known.kt"] == hashlib.sha256(ORIGINAL).hexdigest()
    assert got["HEAD:src/ui/Known.kt"] == graph.baselines["ui/Known.kt"]


def test_blob_digests_skips_a_missing_object_without_desyncing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path added in this change has no blob at an older rev. git answers
    `<spec> missing` with no content, and mis-parsing that line would shift
    every subsequent digest onto the wrong file — silently, and wrongly."""
    monkeypatch.chdir(repo)
    got = g.blob_digests(
        ["HEAD:src/ui/Nope.kt", "HEAD:src/ui/Known.kt", "HEAD:src/ui/AlsoNope.kt"]
    )
    assert got == {"HEAD:src/ui/Known.kt": hashlib.sha256(ORIGINAL).hexdigest()}


def test_blob_digests_of_nothing_spawns_nothing() -> None:
    assert g.blob_digests([]) == {}


def test_digests_for_falls_through_to_the_next_rev(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-ref push resolves each path against the first tip that has it."""
    monkeypatch.chdir(repo)
    got = g.digests_for(["src/ui/Known.kt"], ["nonexistent-rev", "HEAD"])
    assert got == {"src/ui/Known.kt": hashlib.sha256(ORIGINAL).hexdigest()}


def test_digests_for_reads_the_index_with_an_empty_rev(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "src/ui/Known.kt")
    assert g.digests_for(["src/ui/Known.kt"], [""]) == {
        "src/ui/Known.kt": hashlib.sha256(REWRITTEN).hexdigest()
    }


def test_a_path_git_cannot_resolve_yields_no_entry(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    assert g.digests_for(["src/ui/Ghost.kt"], ["HEAD"]) == {}


# --- posture -----------------------------------------------------------------
def test_a_graph_with_no_baselines_reports_no_drift(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built with no source tree in reach, so the `source` table is empty. That
    must read as "no baseline", never as "unchanged" — SCHEMA.md §6.3."""
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="test", root="src", generated="2026-08-01"),
        [Node(
            id="known", kind="Ui", description="Ranks the feed.",
            anchors=[Anchor("ui/Known.kt", "Known")], edges=[], section="UI",
        )],
    )
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "-A")
    err = _run_commit_hook(repo, monkeypatch)
    assert "changed since" not in err


def test_the_check_still_exits_zero_when_it_finds_drift(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted inside `_run_commit_hook`, restated here because it is the one
    property that must never regress: advisory means advisory."""
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "-A")
    assert "Nothing is blocked" in _run_commit_hook(repo, monkeypatch)


def test_skip_kg_silences_the_whole_check(repo: Path) -> None:
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "-A")
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "kg_pre_commit.py")],
        cwd=repo, capture_output=True, check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo), "SKIP_KG": "1"},
    )
    assert proc.returncode == 0 and proc.stderr.decode().strip() == ""


def test_the_commit_hook_imports_one_implementation() -> None:
    """The coverage rule and now the digest rule live in `kg_pre_push.py`; the
    commit hook imports them. Two copies would drift, and only one of them is
    the file `test_hook_parity.py` holds to `codebase_kg/coverage.py`."""
    assert commit_hook.analyze is g.analyze
    assert commit_hook.digests_for is g.digests_for
    assert commit_hook.drift_candidates is g.drift_candidates

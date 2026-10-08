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

import kg_pre_commit as commit_hook
import kg_pre_push as g

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


# --- the report names the change set it is about -----------------------------
def test_the_commit_report_never_announces_a_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_emit` is shared with the push hook and used to hardcode "your push is
    going through", corrected by a line printed underneath — so every commit
    report contradicted its own header two lines later."""
    (repo / "src" / "ui" / "Known.kt").write_bytes(REWRITTEN)
    _git(repo, "add", "-A")
    err = _run_commit_hook(repo, monkeypatch)
    assert "your commit is going through" in err
    assert "staged changes" in err
    assert "push" not in err.lower(), f"commit report mentions a push:\n{err}"


def test_the_push_report_still_says_push(capsys: pytest.CaptureFixture[str]) -> None:
    """The default action is unchanged, so the push hook — which does not pass
    one — keeps its own wording."""
    g._emit(g.Findings(unmapped=["src/ui/New.kt"], deleted=[], drifted=[]), "code_graph.db")
    err = capsys.readouterr().err
    assert "your push is going through" in err
    assert "commits being pushed" in err
    assert "commit is going through" not in err


def test_every_findings_bucket_reaches_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    """Adding the drift bucket to `Findings` is worth nothing if `_emit` drops
    it — the two are edited in different places."""
    g._emit(
        g.Findings(unmapped=["a/New.kt"], deleted=["a/Gone.kt"], drifted=["a/Changed.kt"]),
        "code_graph.db",
    )
    err = capsys.readouterr().err
    assert "a/New.kt" in err and "a/Gone.kt" in err and "a/Changed.kt" in err
    assert "changed since" in err


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


# --- the checkout's line endings are not a change ----------------------------
# The builder hashes the working tree; this hook hashes the git blob. On a repo
# with `text=auto eol=lf` checked out on Windows those differ for every text
# file, so a comparison between them reported drift on all of them and carried
# no information at all. The unit tests could not see it: they wrote LF files,
# where the two happen to agree.
CRLF_ORIGINAL = b"class Known {\r\n    fun rank() = 1\r\n}\r\n"
CRLF_REWRITTEN = b"class Known {\r\n    fun rank() = weightedByRecency()\r\n}\r\n"


@pytest.fixture
def crlf_repo(tmp_path: Path) -> Path:
    """A repo that stores LF and checks out CRLF, with a graph built from the
    working tree the way `codebase-kg-build --source-root` does."""
    repo = tmp_path / "crlf"
    (repo / "src" / "ui").mkdir(parents=True)
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    (repo / "src" / "ui" / "Known.kt").write_bytes(CRLF_ORIGINAL)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    db = repo / "knowledge" / "code_graph.db"
    db.parent.mkdir()
    build(
        db,
        Meta(codebase="test", root="src", generated="2026-09-06"),
        [Node(
            id="known", kind="Ui", description="Ranks the feed.",
            anchors=[Anchor("ui/Known.kt", "Known")], edges=[], section="UI",
        )],
        source_root=repo / "src",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_a_crlf_checkout_does_not_read_as_drifted(crlf_repo: Path) -> None:
    """The regression. The blob is LF, the baseline came from a CRLF working
    tree, and nothing about the file has changed."""
    graph = g.read_graph(crlf_repo / "knowledge" / "code_graph.db")
    assert graph is not None
    baseline = graph.baselines["ui/Known.kt"]
    cwd = os.getcwd()
    try:
        os.chdir(crlf_repo)
        from_git = g.digests_for(["src/ui/Known.kt"], ["HEAD"])["src/ui/Known.kt"]
    finally:
        os.chdir(cwd)
    assert from_git == baseline, "a CRLF checkout must not read as drift"


def test_a_real_edit_in_a_crlf_checkout_still_reports(
    crlf_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folding line endings must not fold away the signal."""
    (crlf_repo / "src" / "ui" / "Known.kt").write_bytes(CRLF_REWRITTEN)
    _git(crlf_repo, "add", "src/ui/Known.kt")
    err = _run_commit_hook(crlf_repo, monkeypatch)
    assert "changed since" in err
    assert "src/ui/Known.kt" in err


def test_only_the_line_endings_changing_is_not_drift(
    crlf_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting the file with LF endings and nothing else is not a change the
    description could be wrong about."""
    (crlf_repo / "src" / "ui" / "Known.kt").write_bytes(CRLF_ORIGINAL.replace(b"\r\n", b"\n"))
    _git(crlf_repo, "add", "src/ui/Known.kt")
    assert "changed since" not in _run_commit_hook(crlf_repo, monkeypatch)

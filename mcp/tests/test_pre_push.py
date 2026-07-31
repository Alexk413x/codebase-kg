"""The vendored pre-push staleness check.

Two properties matter most: it reports the right files, and it never blocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

# The checker is vendorable and lives outside the mcp package.
HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"
sys.path.insert(0, str(HOOKS))

import kg_pre_push as g  # noqa: E402

ZEROS = "0" * 40


# --- advisory posture --------------------------------------------------------
def test_main_never_blocks_even_with_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The whole point of the rewrite: findings are reported, the push proceeds.
    repo = _repo_with_graph(tmp_path)
    monkeypatch.setattr(g, "_git", _fake_git(repo, "A\tsrc/Brand.kt"))
    monkeypatch.setattr(g.sys, "stdin", _Stdin("refs/heads/m aaa refs/heads/m bbb\n"))
    assert g.main() == 0
    assert "Brand.kt" in capsys.readouterr().err


def test_main_is_silent_when_nothing_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo = _repo_with_graph(tmp_path)
    monkeypatch.setattr(g, "_git", _fake_git(repo, "M\tsrc/Known.kt"))
    monkeypatch.setattr(g.sys, "stdin", _Stdin("refs/heads/m aaa refs/heads/m bbb\n"))
    assert g.main() == 0
    assert capsys.readouterr().err == ""


def test_repo_without_a_graph_is_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g, "_git", _fake_git(tmp_path, ""))
    assert g.main() == 0


def test_unreadable_graph_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "code_graph.db").write_text("junk", encoding="utf-8")
    monkeypatch.setattr(g, "_git", _fake_git(tmp_path, "A\tsrc/X.kt"))
    monkeypatch.setattr(g.sys, "stdin", _Stdin("refs/heads/m aaa refs/heads/m bbb\n"))
    assert g.main() == 0
    assert capsys.readouterr().err == ""


# --- analyze (pure) ----------------------------------------------------------
ANCHORED = {"ui/Known.kt", "domain/Ranker.kt"}


def test_new_unmapped_source_is_reported() -> None:
    unmapped, deleted = g.analyze([("A", "src/ui/Brand.kt")], "src", None, ANCHORED)
    assert unmapped == ["src/ui/Brand.kt"] and deleted == []


def test_new_source_that_is_already_anchored_is_not_reported() -> None:
    unmapped, _ = g.analyze([("A", "src/ui/Known.kt")], "src", None, ANCHORED)
    assert unmapped == []


def test_deleted_but_still_anchored_source_is_reported() -> None:
    _, deleted = g.analyze([("D", "src/ui/Known.kt")], "src", None, ANCHORED)
    assert deleted == ["src/ui/Known.kt"]


def test_deleted_unanchored_source_is_not_reported() -> None:
    _, deleted = g.analyze([("D", "src/ui/Scratch.kt")], "src", None, ANCHORED)
    assert deleted == []


def test_modified_source_is_not_reported() -> None:
    # A modification is not evidence of drift on its own; the post-edit hook
    # and kg_validate cover description drift.
    unmapped, deleted = g.analyze([("M", "src/ui/Brand.kt")], "src", None, ANCHORED)
    assert unmapped == [] and deleted == []


def test_paths_outside_root_are_ignored() -> None:
    unmapped, _ = g.analyze([("A", "docs/Notes.kt")], "src", None, ANCHORED)
    assert unmapped == []


def test_the_graph_itself_is_never_a_trigger() -> None:
    unmapped, _ = g.analyze(
        [("A", "knowledge/code_graph.db")], "", "knowledge/code_graph.db", ANCHORED
    )
    assert unmapped == []


# --- is_source ---------------------------------------------------------------
def test_is_source_under_root() -> None:
    assert g.is_source("app/src/main/java/com/x/Foo.kt", "app/src/main/java/com/x", None)


def test_is_source_excludes_the_graph() -> None:
    assert not g.is_source("knowledge/code_graph.db", "", "knowledge/code_graph.db")


def test_is_source_excludes_outside_root() -> None:
    assert not g.is_source("docs/readme.kt", "app/src", None)


def test_is_source_excludes_docs_and_build_dirs() -> None:
    assert not g.is_source("app/src/README.md", "app/src", None)
    assert not g.is_source("app/src/build/Generated.kt", "app/src", None)


# --- _rel_to_root ------------------------------------------------------------
def test_rel_to_root_strips_the_root_prefix() -> None:
    assert g._rel_to_root("app/src/main/Foo.kt", "app/src/main") == "Foo.kt"


def test_rel_to_root_is_identity_without_a_root() -> None:
    assert g._rel_to_root("src/Foo.kt", "") == "src/Foo.kt"


def test_rel_to_root_leaves_paths_outside_root_alone() -> None:
    assert g._rel_to_root("other/Foo.kt", "app/src") == "other/Foo.kt"


# --- changed_files (status parsing) ------------------------------------------
def test_changed_files_parses_status_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "_git", lambda *a: "A\tsrc/New.kt\nM\tsrc/Old.kt\nD\tsrc/Gone.kt\n")
    assert g.changed_files("x...y") == [
        ("A", "src/New.kt"), ("M", "src/Old.kt"), ("D", "src/Gone.kt")
    ]


def test_changed_files_splits_a_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rename is both: the old path may still be anchored, the new one may not
    # be mapped yet.
    monkeypatch.setattr(g, "_git", lambda *a: "R100\tsrc/Old.kt\tsrc/New.kt\n")
    assert g.changed_files("x...y") == [("D", "src/Old.kt"), ("A", "src/New.kt")]


# --- stdin ref parsing -------------------------------------------------------
def test_parse_push_refs_parses_ref_lines() -> None:
    text = (
        "refs/heads/main aaa111 refs/heads/main bbb222\n"
        "\n"
        "garbage\n"
        f"refs/heads/gone {ZEROS} refs/heads/gone ccc333\n"
    )
    assert g.parse_push_refs(text) == [
        ("refs/heads/main", "aaa111", "refs/heads/main", "bbb222"),
        ("refs/heads/gone", ZEROS, "refs/heads/gone", "ccc333"),
    ]


def test_parse_push_refs_empty_stdin() -> None:
    assert g.parse_push_refs("") == []


# --- changed_files_for_push --------------------------------------------------
def test_push_to_existing_ref_diffs_remote_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        return "A\tsrc/A.kt\nM\tsrc/B.kt\n" if args[0] == "diff" else ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/x", "aaa111", "refs/heads/x", "bbb222")]
    # The pushed branch's own range — NOT the checked-out branch, NOT @{u}..HEAD.
    assert g.changed_files_for_push(refs) == [("A", "src/A.kt"), ("M", "src/B.kt")]
    assert ("diff", "--name-status", "bbb222...aaa111") in calls


def test_new_branch_push_diffs_from_remote_default_merge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(*args: str) -> str:
        if args[0] == "merge-base" and args[1] == "origin/HEAD":
            return "base123\n"
        if args[0] == "diff" and args[-1] == "base123..aaa111":
            return "A\tsrc/New.kt\n"
        return ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/new", "aaa111", "refs/heads/new", ZEROS)]
    assert g.changed_files_for_push(refs) == [("A", "src/New.kt")]


def test_new_branch_without_remote_base_lists_unpushed_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return ""  # no origin/HEAD|main|master at all
        if args[0] == "rev-list":
            return "c1\nc2\n"
        if args[0] == "diff-tree":
            return {"c1": "A\tsrc/A.kt\n", "c2": "A\tsrc/A.kt\nA\tsrc/B.kt\n"}[args[-1]]
        return ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/new", "aaa111", "refs/heads/new", ZEROS)]
    assert g.changed_files_for_push(refs) == [("A", "src/A.kt"), ("A", "src/B.kt")]


def test_deleted_ref_push_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        raise AssertionError("a ref deletion pushes no commits — no git calls expected")

    monkeypatch.setattr(g, "_git", fake_git)
    assert g.changed_files_for_push([("(delete)", ZEROS, "refs/heads/x", "bbb222")]) == []


# --- push_range fallback (manual invocation, no stdin) -----------------------
def test_push_range_prefers_upstream_three_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        return "origin/main\n" if args[0] == "rev-parse" and "--abbrev-ref" in args else ""

    monkeypatch.setattr(g, "_git", fake_git)
    assert g.push_range() == "@{u}...HEAD"


def test_push_range_none_when_no_base_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "_git", lambda *a: "")
    assert g.push_range() is None


# --- graph reading -----------------------------------------------------------
def test_read_graph_returns_root_anchors_and_the_coverage_declaration(tmp_path: Path) -> None:
    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [Node(id="a", kind="K", anchors=[Anchor("ui/Known.kt", "Known")])],
    )
    assert g.read_graph(db) == ("src", {"ui/Known.kt"}, [], [])


def test_read_graph_of_a_non_database_is_none(tmp_path: Path) -> None:
    junk = tmp_path / "code_graph.db"
    junk.write_text("nope", encoding="utf-8")
    assert g.read_graph(junk) is None


def test_find_graph_rel_honors_legacy_kg_path(tmp_path: Path) -> None:
    (tmp_path / "custom").mkdir()
    db = tmp_path / "custom" / "g.db"
    build(db, Meta(codebase="x"), [Node(id="a", kind="K")])
    assert g.find_graph_rel(tmp_path, {"kg_path": "custom/g.db"}) == "custom/g.db"


def test_find_graph_rel_none_when_absent(tmp_path: Path) -> None:
    assert g.find_graph_rel(tmp_path, {}) is None


# --- helpers -----------------------------------------------------------------
class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text


def _repo_with_graph(tmp_path: Path) -> Path:
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [Node(id="a", kind="K", anchors=[Anchor("Known.kt", "Known")])],
    )
    return tmp_path


def _fake_git(repo: Path, diff_output: str):
    def fake_git(*args: str) -> str:
        if args[0] == "rev-parse" and "--show-toplevel" in args:
            return str(repo) + "\n"
        if args[0] == "diff":
            return diff_output + "\n" if diff_output else ""
        return ""

    return fake_git

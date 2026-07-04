from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The pre-push checker is vendorable and lives outside the mcp package.
HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"
sys.path.insert(0, str(HOOKS))

import kg_pre_push as g  # noqa: E402


# --- gate_decision (pure) --------------------------------------------------
def test_gate_no_source_change_passes() -> None:
    assert g.gate_decision(source_changed=False, kg_in_changeset=False, kg_fresh=False)[0] is False


def test_gate_source_without_kg_blocks() -> None:
    block, reason = g.gate_decision(True, kg_in_changeset=False, kg_fresh=False)
    assert block is True and "not updated" in reason


def test_gate_source_with_stale_kg_blocks() -> None:
    block, reason = g.gate_decision(True, kg_in_changeset=True, kg_fresh=False)
    assert block is True and "not today" in reason


def test_gate_source_with_fresh_kg_passes() -> None:
    assert g.gate_decision(True, kg_in_changeset=True, kg_fresh=True)[0] is False


# --- is_source -------------------------------------------------------------
def test_is_source_under_root() -> None:
    assert g.is_source("app/src/main/java/com/x/Foo.kt", "app/src/main/java/com/x", "knowledge/KNOWLEDGE_GRAPH.md")


def test_is_source_excludes_kg_itself() -> None:
    assert not g.is_source("knowledge/KNOWLEDGE_GRAPH.md", "", "knowledge/KNOWLEDGE_GRAPH.md")


def test_is_source_excludes_outside_root() -> None:
    assert not g.is_source("docs/readme.kt", "app/src", None)


def test_is_source_excludes_docs_and_build() -> None:
    assert not g.is_source("app/src/README.md", "app/src", None)
    assert not g.is_source("app/src/build/Generated.kt", "app/src", None)


# --- kg_refreshed_today ----------------------------------------------------
def test_refreshed_today_structured(tmp_path: Path) -> None:
    kg = tmp_path / "KNOWLEDGE_GRAPH.md"
    kg.write_text("# T\n\n```\ncodebase: x\nrefreshed:   2026-06-09\n```\n", encoding="utf-8")
    assert g.kg_refreshed_today(kg, "2026-06-09") is True
    assert g.kg_refreshed_today(kg, "2026-06-08") is False


def test_refreshed_today_legacy_phrase(tmp_path: Path) -> None:
    kg = tmp_path / "KNOWLEDGE_GRAPH.md"
    kg.write_text("# T\n\nBorn 2025-01-01; last refreshed 2026-06-09 — note.\n", encoding="utf-8")
    assert g.kg_refreshed_today(kg, "2026-06-09") is True


def test_kg_header_value_reads_root_from_header(tmp_path: Path) -> None:
    # The committed KG header is the shared config — root comes from here, not a .local.md.
    kg = tmp_path / "KNOWLEDGE_GRAPH.md"
    kg.write_text("# T\n\n```\ncodebase: x\nroot: app/src/main\ncounterpart: ../y\n```\n", encoding="utf-8")
    assert g.kg_header_value(kg, "root") == "app/src/main"
    assert g.kg_header_value(kg, "counterpart") == "../y"
    assert g.kg_header_value(kg, "missing") == ""


# --- stdin ref parsing -------------------------------------------------------
ZEROS = "0" * 40


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
        if args[0] == "diff":
            return "src/A.kt\nsrc/B.kt\n"
        return ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/x", "aaa111", "refs/heads/x", "bbb222")]
    # The pushed branch's own range — NOT the checked-out branch, NOT @{u}..HEAD.
    assert g.changed_files_for_push(refs) == ["src/A.kt", "src/B.kt"]
    assert ("diff", "--name-only", "bbb222...aaa111") in calls


def test_new_branch_push_diffs_from_remote_default_merge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        if args[0] == "merge-base" and args[1] == "origin/HEAD":
            return "base123\n"
        if args[0] == "diff" and args[-1] == "base123..aaa111":
            return "src/New.kt\n"
        return ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/new", "aaa111", "refs/heads/new", ZEROS)]
    assert g.changed_files_for_push(refs) == ["src/New.kt"]


def test_new_branch_push_without_remote_base_lists_unpushed_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return ""  # no origin/HEAD|main|master at all
        if args[0] == "rev-list":
            return "c1\nc2\n"
        if args[0] == "diff-tree":
            return {"c1": "src/A.kt\n", "c2": "src/A.kt\nsrc/B.kt\n"}[args[-1]]
        return ""

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("refs/heads/new", "aaa111", "refs/heads/new", ZEROS)]
    # union across commits, deduped
    assert g.changed_files_for_push(refs) == ["src/A.kt", "src/B.kt"]


def test_deleted_ref_push_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        raise AssertionError("a ref deletion pushes no commits — no git calls expected")

    monkeypatch.setattr(g, "_git", fake_git)
    refs = [("(delete)", ZEROS, "refs/heads/x", "bbb222")]
    assert g.changed_files_for_push(refs) == []


# --- push_range fallback (manual invocation, no stdin) -----------------------
def test_push_range_prefers_upstream_three_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str:
        return "origin/main\n" if args[0] == "rev-parse" and "--abbrev-ref" in args else ""

    monkeypatch.setattr(g, "_git", fake_git)
    assert g.push_range() == "@{u}...HEAD"


def test_push_range_none_when_no_base_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # No upstream, no origin/main|master: the caller must fail loudly, never
    # silently pass on a guessed empty range.
    monkeypatch.setattr(g, "_git", lambda *a: "")
    assert g.push_range() is None

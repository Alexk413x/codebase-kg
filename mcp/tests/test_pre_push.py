from __future__ import annotations

import sys
from pathlib import Path

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

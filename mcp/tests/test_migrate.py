"""Migration: markdown in, committed store out, structure intact."""

from __future__ import annotations

from pathlib import Path

import pytest
from codebase_kg import markdown, migrate
from codebase_kg.models import Node
from codebase_kg.store import CodeGraph

FIX = Path(__file__).resolve().parent / "fixtures"


def _node(graph: CodeGraph, node_id: str) -> Node:
    """Fetch a node the test asserts exists, without an Optional at every use."""
    n = graph.node(node_id)
    assert n is not None, f"no node {node_id!r}"
    return n


# --- fidelity ----------------------------------------------------------------
def test_every_node_survives(built_fixtures: Path) -> None:
    for name in ("android", "ios", "wide"):
        _, parsed = markdown.load(FIX / name / "KNOWLEDGE_GRAPH.md")
        g = CodeGraph(built_fixtures / name / "code_graph.db")
        try:
            assert g.counts()["nodes"] == len(parsed), name
        finally:
            g.close()


def test_anchors_survive_exactly(built_fixtures: Path) -> None:
    _, parsed = markdown.load(FIX / "android" / "KNOWLEDGE_GRAPH.md")
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        assert g.counts()["anchors"] == sum(len(n.anchors) for n in parsed)
    finally:
        g.close()


def test_parity_fields_survive(built_fixtures: Path) -> None:
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        ranker = _node(g, "feed_ranker")
        assert ranker.parity == "divergent"
        assert ranker.divergence is not None
        assert _node(g, "night_digest").parity == "android-only"
    finally:
        g.close()


def test_sections_survive(built_fixtures: Path) -> None:
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        assert _node(g, "saved_article").section == "CORE DOMAIN MODEL"
    finally:
        g.close()


# --- what migration changes on purpose ---------------------------------------
def test_dangling_edge_is_dropped_and_reported(tmp_path: Path) -> None:
    r = migrate.convert(FIX / "android" / "KNOWLEDGE_GRAPH.md", tmp_path / "g.db")
    assert {"src": "bookmarks_screen", "dst": "ghost_node"} in r.dropped_edges


def test_counterpart_is_retargeted_to_the_new_store(built_fixtures: Path) -> None:
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        assert _node(g, "saved_article").counterpart == "../ios/code_graph.db#saved_article"
    finally:
        g.close()


def test_meta_counterpart_is_retargeted(built_fixtures: Path) -> None:
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        assert g.meta.counterpart == "../ios/code_graph.db"
    finally:
        g.close()


def test_ticket_refs_are_stripped_from_descriptions(built_fixtures: Path) -> None:
    g = CodeGraph(built_fixtures / "wide" / "code_graph.db")
    try:
        desc = _node(g, "home-page").description
        assert "ACME-901" not in desc
        assert "Top | Recent | For You" in desc  # content kept, history removed
    finally:
        g.close()


def test_report_counts_what_it_scrubbed(tmp_path: Path) -> None:
    r = migrate.convert(FIX / "wide" / "KNOWLEDGE_GRAPH.md", tmp_path / "g.db")
    assert r.descriptions_scrubbed >= 1
    assert r.chars_removed > 0
    assert r.needs_rewrite == []


def test_source_markdown_is_never_modified(tmp_path: Path) -> None:
    src = FIX / "android" / "KNOWLEDGE_GRAPH.md"
    before = src.read_bytes()
    migrate.convert(src, tmp_path / "g.db")
    assert src.read_bytes() == before


# --- normalization of shapes the store refuses -------------------------------
def _normalize(node: Node) -> tuple[Node, migrate.MigrationReport]:
    report = migrate.MigrationReport()
    migrate._normalize_parity(node, report)
    return node, report


def test_matched_without_counterpart_loses_its_parity() -> None:
    node, report = _normalize(Node(id="a", kind="K", parity="matched"))
    assert node.parity is None
    assert "no counterpart" in report.normalized[0]["issue"]


def test_only_with_counterpart_loses_the_counterpart() -> None:
    node, report = _normalize(
        Node(id="a", kind="K", parity="ios-only", counterpart="../p.db#x")
    )
    assert node.parity == "ios-only" and node.counterpart is None
    assert report.normalized


def test_divergent_without_divergence_line_loses_its_parity() -> None:
    node, _ = _normalize(Node(id="a", kind="K", parity="divergent", counterpart="../p.db#x"))
    assert node.parity is None and node.counterpart is None


def test_counterpart_without_parity_is_dropped() -> None:
    node, _ = _normalize(Node(id="a", kind="K", counterpart="../p.db#x"))
    assert node.counterpart is None


def test_unrecognized_parity_is_dropped() -> None:
    node, _ = _normalize(Node(id="a", kind="K", parity="probably-fine"))
    assert node.parity is None


def test_line_number_anchor_is_reduced_to_its_path() -> None:
    from codebase_kg.models import Anchor

    report = migrate.MigrationReport()
    node = Node(id="a", kind="K", anchors=[Anchor("x.kt", "42")])
    migrate._normalize_anchors(node, report)
    assert node.anchors == [Anchor("x.kt", None)]
    assert report.normalized


def test_node_without_a_kind_gets_a_placeholder(tmp_path: Path) -> None:
    src = tmp_path / "KNOWLEDGE_GRAPH.md"
    src.write_text("## NODES\n\n| id | a |\n| summary | No kind here. |\n", encoding="utf-8")
    r = migrate.convert(src, tmp_path / "g.db")
    assert any("no kind" in n["issue"] for n in r.normalized)
    g = CodeGraph(tmp_path / "g.db")
    try:
        assert _node(g, "a").kind == "unknown"
    finally:
        g.close()


# --- CLI ---------------------------------------------------------------------
def test_cli_writes_beside_the_source_by_default(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(FIX / "android", tmp_path / "android")
    src = tmp_path / "android" / "KNOWLEDGE_GRAPH.md"
    assert migrate.main([str(src)]) == 0
    assert (tmp_path / "android" / "code_graph.db").is_file()


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(FIX / "android", tmp_path / "android")
    src = tmp_path / "android" / "KNOWLEDGE_GRAPH.md"
    assert migrate.main([str(src), "--dry-run"]) == 0
    assert not (tmp_path / "android" / "code_graph.db").exists()


def test_cli_reports_a_missing_source(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert migrate.main([str(tmp_path / "nope.md")]) == 1
    assert "no such file" in capsys.readouterr().err

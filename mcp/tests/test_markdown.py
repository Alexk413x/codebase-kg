"""The legacy markdown parser — migration only, but it has to be right once.

Covers both table shapes found in real graphs. The wide shape is the one the
original vertical-only parser silently mis-read: every row of a wide table
produced a node called `kind`, so a 162-node graph loaded as one useless node.
"""

from __future__ import annotations

from pathlib import Path

from codebase_kg import markdown

FIX = Path(__file__).resolve().parent / "fixtures"


# --- vertical form -----------------------------------------------------------
def test_vertical_tables_parse() -> None:
    meta, nodes = markdown.load(FIX / "android" / "KNOWLEDGE_GRAPH.md")
    assert meta.codebase == "android"
    assert meta.root == "src"
    assert meta.generated == "2026-06-08"  # `refreshed:` becomes `generated`
    assert [n.id for n in nodes] == [
        "saved_article", "bookmarks_screen", "feed_ranker", "night_digest",
    ]


def test_vertical_fields_and_section() -> None:
    _, nodes = markdown.load(FIX / "android" / "KNOWLEDGE_GRAPH.md")
    by_id = {n.id: n for n in nodes}
    ranker = by_id["feed_ranker"]
    assert ranker.kind == "Domain (pure)"
    assert ranker.section == "DOMAIN"
    assert [str(a) for a in ranker.anchors] == ["domain/FeedRanker.kt#FeedRanker"]
    assert ranker.parity == "divergent"
    assert ranker.counterpart == "../ios/KNOWLEDGE_GRAPH.md#personalized_ranking"
    assert ranker.divergence is not None


def test_dangling_edge_survives_parsing_for_the_migrator_to_handle() -> None:
    _, nodes = markdown.load(FIX / "android" / "KNOWLEDGE_GRAPH.md")
    screen = next(n for n in nodes if n.id == "bookmarks_screen")
    assert "ghost_node" in screen.edges


# --- wide form ---------------------------------------------------------------
def test_wide_tables_parse_one_node_per_row() -> None:
    meta, nodes = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    assert meta.codebase == "web"
    assert [n.id for n in nodes] == ["app-router", "home-page", "saved-page", "article-card"]
    assert not any(n.id == "kind" for n in nodes)  # the old failure mode


def test_wide_fields_map_positionally() -> None:
    _, nodes = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    router = next(n for n in nodes if n.id == "app-router")
    assert router.kind == "Router"
    assert [str(a) for a in router.anchors] == ["routes/Router.tsx#Router"]
    assert router.edges == ["home-page", "saved-page"]
    assert router.parity == "web-only"
    assert router.section == "ROUTES"


def test_wide_row_with_literal_pipes_in_prose_is_not_truncated() -> None:
    _, nodes = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    home = next(n for n in nodes if n.id == "home-page")
    assert "Top | Recent | For You" in home.description
    assert home.edges == ["article-card"]  # the columns after the prose still line up


def test_wide_header_repeats_per_section() -> None:
    _, nodes = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    sections = {n.id: n.section for n in nodes}
    assert sections["app-router"] == "ROUTES"
    assert sections["article-card"] == "COMPONENTS"


def test_empty_trailing_cell_is_not_a_value() -> None:
    _, nodes = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    card = next(n for n in nodes if n.id == "article-card")
    assert card.edges == []


# --- header placeholders -----------------------------------------------------
def test_none_counterpart_placeholder_reads_as_absent() -> None:
    meta, _ = markdown.load(FIX / "wide" / "KNOWLEDGE_GRAPH.md")
    assert meta.counterpart is None


def test_template_placeholder_reads_as_absent() -> None:
    meta, _ = markdown.parse("# T\n\n```\ncodebase: x\ncounterpart: <peer path>\n```\n")
    assert meta.counterpart is None


# --- disambiguation ----------------------------------------------------------
def test_a_vertical_id_row_is_not_mistaken_for_a_wide_header() -> None:
    # `| id | kind |` is ambiguous on its own; requiring 3+ alias columns keeps
    # a vertical node whose id happens to be a field name parsing correctly.
    _, nodes = markdown.parse(
        "## NODES\n\n| id | kind |\n| kind | Thing |\n| summary | A node named kind. |\n"
    )
    assert [n.id for n in nodes] == ["kind"]
    assert nodes[0].kind == "Thing"


def test_formatter_separator_row_does_not_end_a_node() -> None:
    _, nodes = markdown.parse(
        "## NODES\n\n| id | a |\n|---|---|\n| kind | K |\n| summary | S. |\n"
    )
    assert nodes[0].kind == "K" and nodes[0].description == "S."


def test_legacy_field_aliases_are_accepted() -> None:
    _, nodes = markdown.parse(
        "## NODES\n\n| id | a |\n| type | K |\n| files | `x.kt#X` |\n"
        "| details | Legacy names. |\n| deps | b |\n"
    )
    n = nodes[0]
    assert n.kind == "K" and n.description == "Legacy names."
    assert [str(a) for a in n.anchors] == ["x.kt#X"] and n.edges == ["b"]

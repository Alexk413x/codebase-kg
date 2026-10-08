"""The MCP tool surface: shapes, ranking, and the advisory drift report."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import tools
from codebase_kg.models import Anchor
from codebase_kg.store import CodeGraph


# --- kg_search ---------------------------------------------------------------
def test_search_ranks_the_named_node_first(android_graph: CodeGraph) -> None:
    r = tools.kg_search(android_graph, "feed ranker")
    assert r["results"][0]["id"] == "feed_ranker"


def test_search_matches_description_text(android_graph: CodeGraph) -> None:
    r = tools.kg_search(android_graph, "swipe to delete")
    assert [x["id"] for x in r["results"]] == ["bookmarks_screen"]


def test_search_results_carry_anchors(android_graph: CodeGraph) -> None:
    r = tools.kg_search(android_graph, "feed ranker")
    assert r["results"][0]["anchors"] == ["domain/FeedRanker.kt#FeedRanker"]


def test_search_kind_filter(android_graph: CodeGraph) -> None:
    r = tools.kg_search(android_graph, "bookmark", kind="Composable")
    assert {x["id"] for x in r["results"]} == {"bookmarks_screen"}


def test_search_kind_filter_excluding_everything_is_empty(
    android_graph: CodeGraph,
) -> None:
    assert tools.kg_search(android_graph, "bookmark", kind="nope")["results"] == []


def test_search_miss_is_empty_not_an_error(android_graph: CodeGraph) -> None:
    r = tools.kg_search(android_graph, "quantum tunnelling")
    assert r["count"] == 0 and r["results"] == []


def test_search_respects_the_limit(android_graph: CodeGraph) -> None:
    assert len(tools.kg_search(android_graph, "bookmark", limit=1)["results"]) <= 1


def test_search_prefers_the_id_over_a_passing_mention(android_graph: CodeGraph) -> None:
    # `saved_article` is named by other nodes' descriptions and edges; the node
    # whose id *is* the query still has to win.
    r = tools.kg_search(android_graph, "saved article")
    assert r["results"][0]["id"] == "saved_article"


def test_search_exposes_description_not_summary(android_graph: CodeGraph) -> None:
    top = tools.kg_search(android_graph, "feed ranker")["results"][0]
    assert "description" in top and "summary" not in top


# --- kg_node -----------------------------------------------------------------
def test_node_returns_the_full_record(android_graph: CodeGraph) -> None:
    n = tools.kg_node(android_graph, "feed_ranker")
    assert n["found"] is True
    assert n["kind"] == "Domain (pure)"
    assert n["anchors"] == ["domain/FeedRanker.kt#FeedRanker"]
    assert n["parity"] == "divergent"


def test_node_reports_inbound_edges(android_graph: CodeGraph) -> None:
    assert tools.kg_node(android_graph, "saved_article")["inbound_edges"] == [
        "bookmarks_screen",
        "feed_ranker",
    ]


def test_unknown_node_suggests_alternatives(android_graph: CodeGraph) -> None:
    n = tools.kg_node(android_graph, "ranker")
    assert n["found"] is False
    assert "feed_ranker" in n["did_you_mean"]


# --- kg_neighborhood ---------------------------------------------------------
def test_neighborhood_includes_both_directions(android_graph: CodeGraph) -> None:
    r = tools.kg_neighborhood(android_graph, "saved_article", depth=1)
    assert {n["id"] for n in r["neighbors"]} == {"bookmarks_screen", "feed_ranker"}
    assert all(n["hops"] == 1 for n in r["neighbors"])


def test_neighborhood_depth_is_clamped(android_graph: CodeGraph) -> None:
    assert tools.kg_neighborhood(android_graph, "saved_article", depth=99)["depth"] == 3
    assert tools.kg_neighborhood(android_graph, "saved_article", depth=0)["depth"] == 1


def test_neighborhood_of_an_unknown_node(android_graph: CodeGraph) -> None:
    assert tools.kg_neighborhood(android_graph, "nope")["found"] is False


def test_neighborhood_never_lists_a_dangling_target(android_graph: CodeGraph) -> None:
    # `ghost_node` was in the markdown; the store makes it unwritable, so it
    # can no longer show up as an unresolved neighbor the way it used to.
    r = tools.kg_neighborhood(android_graph, "bookmarks_screen", depth=2)
    assert "ghost_node" not in {n["id"] for n in r["neighbors"]}


# --- kg_find_by_kind ---------------------------------------------------------
def test_find_by_kind_substring(android_graph: CodeGraph) -> None:
    r = tools.kg_find_by_kind(android_graph, "entity")
    assert r["count"] == 1 and r["nodes"][0]["id"] == "saved_article"


def test_find_by_kind_miss(android_graph: CodeGraph) -> None:
    assert tools.kg_find_by_kind(android_graph, "actor")["count"] == 0


# --- kg_find_by_path ---------------------------------------------------------
def test_find_by_path_full_path(android_graph: CodeGraph) -> None:
    r = tools.kg_find_by_path(android_graph, "domain/FeedRanker.kt")
    assert [n["id"] for n in r["nodes"]] == ["feed_ranker"]


def test_find_by_path_bare_filename(android_graph: CodeGraph) -> None:
    r = tools.kg_find_by_path(android_graph, "BookmarksScreen.kt")
    assert [n["id"] for n in r["nodes"]] == ["bookmarks_screen"]


def test_find_by_path_reports_matching_anchors_and_edges(
    android_graph: CodeGraph,
) -> None:
    node = tools.kg_find_by_path(android_graph, "SavedArticleEntity.kt")["nodes"][0]
    assert node["matched_anchors"] == ["room/SavedArticleEntity.kt#SavedArticleEntity"]
    assert node["inbound_edges"] == ["bookmarks_screen", "feed_ranker"]


def test_find_by_path_miss(android_graph: CodeGraph) -> None:
    assert tools.kg_find_by_path(android_graph, "Nothing.kt")["count"] == 0


def test_find_by_path_tolerates_backslashes(android_graph: CodeGraph) -> None:
    r = tools.kg_find_by_path(android_graph, "domain\\FeedRanker.kt")
    assert [n["id"] for n in r["nodes"]] == ["feed_ranker"]


# --- kg_parity_gaps ----------------------------------------------------------
def test_parity_gaps_lists_divergent_and_only(android_graph: CodeGraph) -> None:
    r = tools.kg_parity_gaps(android_graph)
    assert {g["id"] for g in r["gaps"]} == {"feed_ranker", "night_digest"}
    assert r["by_status"] == {"divergent": 1, "android-only": 1}


def test_parity_gaps_excludes_matched(android_graph: CodeGraph) -> None:
    ids = {g["id"] for g in tools.kg_parity_gaps(android_graph)["gaps"]}
    assert "saved_article" not in ids


def test_parity_gaps_status_filter(android_graph: CodeGraph) -> None:
    only = lambda s: [g["id"] for g in tools.kg_parity_gaps(android_graph, s)["gaps"]]
    assert only("divergent") == ["feed_ranker"]
    assert only("only") == ["night_digest"]
    assert only("android-only") == ["night_digest"]


# --- kg_stats ----------------------------------------------------------------
def test_stats_reports_totals_and_breakdowns(android_graph: CodeGraph) -> None:
    s = tools.kg_stats(android_graph)
    assert s["codebase"] == "android"
    assert s["nodes"] == 4
    assert s["anchors"] == 4
    assert s["parity"] == {"matched": 1, "divergent": 1, "android-only": 1}
    assert s["kinds"]["Room @Entity"] == 1


def test_stats_flags_isolated_nodes(android_graph: CodeGraph) -> None:
    # night_digest has no edge in either direction.
    assert tools.kg_stats(android_graph)["isolated_nodes"]["ids"] == ["night_digest"]


# --- kg_validate -------------------------------------------------------------
def test_validate_resolves_the_source_base(android_graph: CodeGraph) -> None:
    v = tools.kg_validate(android_graph)
    assert v["source_checked"] is True
    assert v["anchors_checked"] == 4


def test_validate_flags_an_ungreppable_symbol(android_graph: CodeGraph) -> None:
    issues = tools.kg_validate(android_graph)["anchor_issues"]
    assert {
        "node": "night_digest",
        "anchor": "theme/Theme.kt#MissingSymbolXYZ",
        "issue": "symbol not found in file",
    } in issues


def test_validate_is_always_advisory(android_graph: CodeGraph) -> None:
    assert tools.kg_validate(android_graph)["advisory"] is True


def test_validate_reports_structural_guarantees_instead_of_checking(
    android_graph: CodeGraph,
) -> None:
    v = tools.kg_validate(android_graph)
    assert any("dangling" in g for g in v["guaranteed_by_schema"])
    assert "dangling_edges" not in v and "duplicate_ids" not in v


def test_validate_flags_a_non_reciprocal_counterpart(
    ios_graph: CodeGraph, built_fixtures: Path
) -> None:
    peer = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        issues = tools.kg_validate(ios_graph, peer)["counterpart_issues"]
        # iOS `bookmarks_view_model` points at android `bookmarks_screen`,
        # which points back at nothing — the link is one-directional.
        assert any(
            i["node"] == "bookmarks_view_model" and "back-link" in i["issue"]
            for i in issues
        )
    finally:
        peer.close()


def test_validate_accepts_a_reciprocal_counterpart(
    ios_graph: CodeGraph, built_fixtures: Path
) -> None:
    peer = CodeGraph(built_fixtures / "android" / "code_graph.db")
    try:
        issues = tools.kg_validate(ios_graph, peer)["counterpart_issues"]
        assert not any(i["node"] == "saved_article" for i in issues)
    finally:
        peer.close()


def test_validate_without_a_peer_skips_reciprocity(android_graph: CodeGraph) -> None:
    v = tools.kg_validate(android_graph, None)
    assert all("reciprocal" not in i["issue"] for i in v["counterpart_issues"])


# --- coverage ----------------------------------------------------------------
def test_undeclared_coverage_falls_back_to_anchored_extensions_and_says_so(
    android_graph: CodeGraph, built_fixtures: Path
) -> None:
    cov = tools.coverage_report(android_graph, built_fixtures / "android" / "src")
    # No `covers` in this fixture, so the old inferred rule still applies...
    assert all(m.endswith(".kt") for m in cov.gaps)
    # ...but the report must admit that it cannot see a file type with no
    # coverage at all, rather than presenting an incomplete answer as complete.
    assert cov.declared is False
    assert "invisible" in str(cov.to_dict()["warning"])


def test_declared_coverage_reports_a_file_type_with_zero_coverage(tmp_path: Path) -> None:
    """The regression this whole mechanism exists for.

    Under the inferred rule a category nothing anchors contributes no extension,
    so it is never examined and never reported. On the real RPN graph that hid
    34 XML files, 3 Gradle scripts and a version catalog behind a green check.
    """
    from codebase_kg.models import Anchor, Meta, Node
    from codebase_kg.writer import build

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.kt").write_text("class Main", encoding="utf-8")
    (tmp_path / "build.gradle.kts").write_text("plugins {}", encoding="utf-8")
    (tmp_path / "icon.png").write_bytes(b"\x89PNG")

    db = tmp_path / "g.db"
    meta = Meta(
        codebase="x",
        root="",
        generated="2026-07-30",
        covers=["src/**/*.kt", "**/*.gradle.kts"],
    )
    build(db, meta, [Node(id="main", kind="K", anchors=[Anchor("src/Main.kt", "Main")])])
    g = CodeGraph(db)
    try:
        cov = tools.coverage_report(g, tmp_path)
        assert cov.declared is True
        assert cov.covered == 1
        assert cov.gaps == ["build.gradle.kts"]  # invisible before this change
        assert cov.out_of_scope == 2  # icon.png and the graph itself
    finally:
        g.close()


def test_exempt_subtracts_from_covers(tmp_path: Path) -> None:
    from codebase_kg.models import Anchor, Meta, Node
    from codebase_kg.writer import build

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.kt").write_text("class Main", encoding="utf-8")
    (tmp_path / "src" / "GeneratedThing.kt").write_text("class G", encoding="utf-8")

    db = tmp_path / "g.db"
    meta = Meta(
        codebase="x",
        root="",
        generated="2026-07-30",
        covers=["src/**/*.kt"],
        exempt=["**/Generated*.kt"],
    )
    build(db, meta, [Node(id="main", kind="K", anchors=[Anchor("src/Main.kt", "Main")])])
    g = CodeGraph(db)
    try:
        cov = tools.coverage_report(g, tmp_path)
        assert cov.gaps == []
        assert cov.exempt == 1
    finally:
        g.close()


def test_coverage_is_empty_without_anchors_or_declaration(tmp_path: Path) -> None:
    from codebase_kg.models import Meta, Node
    from codebase_kg.writer import build

    db = tmp_path / "g.db"
    build(db, Meta(codebase="x", root="", generated="2026-07-30"), [Node(id="a", kind="K")])
    g = CodeGraph(db)
    try:
        assert tools.coverage_report(g, tmp_path).gaps == []
    finally:
        g.close()


# --- result bounds on a large graph -------------------------------------------
LARGE = 2000


@pytest.fixture(scope="module")
def large_graph(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CodeGraph]:
    """One hub pointing at 2,000 nodes that share a file, a parity flag and a doc host."""
    from codebase_kg.models import Meta, Node, Reference
    from codebase_kg.writer import build

    spokes = [
        Node(
            id=f"n{i:04d}",
            kind="Service",
            description="A generated service that sizes a result page.",
            anchors=[Anchor("src/Common.kt", f"Service{i}")],
            parity="divergent",
            counterpart=f"../peer/code_graph.db#p{i}",
            divergence="Differs on the peer.",
            references=[Reference(url=f"https://docs.example.com/api/{i}", kind="spec")],
        )
        for i in range(LARGE)
    ]
    hub = Node(id="hub", kind="Hub", edges=[n.id for n in spokes])
    db = tmp_path_factory.mktemp("large") / "code_graph.db"
    build(db, Meta(codebase="large", root="src", generated="2026-09-28"), [hub, *spokes])
    g = CodeGraph(db)
    yield g
    g.close()


def _bounded(result: dict[str, Any], key: str) -> None:
    assert len(result[key]) == result["count"] == tools.DEFAULT_LIMIT
    assert result["total"] == LARGE and result["truncated"] is True
    assert result["next_offset"] == tools.DEFAULT_LIMIT and "hint" in result
    assert len(json.dumps(result)) < 30_000


def test_list_tools_are_bounded_on_a_large_graph(large_graph: CodeGraph) -> None:
    _bounded(tools.kg_find_by_kind(large_graph, "service"), "nodes")
    _bounded(tools.kg_find_by_path(large_graph, "Common.kt"), "nodes")
    _bounded(tools.kg_find_by_reference(large_graph, "docs.example.com"), "references")
    _bounded(tools.kg_neighborhood(large_graph, "hub", depth=1), "neighbors")
    gaps = tools.kg_parity_gaps(large_graph)
    _bounded(gaps, "gaps")
    assert gaps["by_status"] == {"divergent": LARGE}


def test_offset_pages_to_the_end(large_graph: CodeGraph) -> None:
    last = tools.kg_find_by_kind(large_graph, "service", limit=100, offset=LARGE - 100)
    assert last["count"] == 100 and last["truncated"] is False
    assert "next_offset" not in last and "hint" not in last
    assert last["nodes"][-1]["id"] == f"n{LARGE - 1:04d}"
    seen = [
        n["id"]
        for offset in range(0, LARGE, tools.MAX_LIMIT)
        for n in tools.kg_find_by_kind(large_graph, "service", tools.MAX_LIMIT, offset)["nodes"]
    ]
    assert seen == [f"n{i:04d}" for i in range(LARGE)]


def test_validate_issue_lists_are_capped_with_counts(large_graph: CodeGraph) -> None:
    full = tools.kg_validate(large_graph)
    assert len(full["counterpart_issues"]) == LARGE
    capped = tools.cap_issues(full)
    assert len(capped["counterpart_issues"]) == tools.DEFAULT_LIMIT
    assert capped["issue_counts"]["counterpart_issues"] == LARGE
    assert capped["truncated"] is True and "counterpart_issues" in capped["hint"]
    assert capped["ok"] is full["ok"] is False
    assert len(json.dumps(capped)) < 30_000


def test_validate_cap_leaves_short_lists_whole(android_graph: CodeGraph) -> None:
    full = tools.kg_validate(android_graph)
    capped = tools.cap_issues(full)
    assert capped["truncated"] is False and "hint" not in capped
    for key in tools.ISSUE_LISTS:
        assert capped[key] == full[key]
        assert capped["issue_counts"][key] == len(full[key])

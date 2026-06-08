from __future__ import annotations

from codebase_kg import tools
from codebase_kg.loader import parse_graph
from codebase_kg.models import Graph


def _ids(results: list[dict[str, object]]) -> set[str]:
    return {str(r["id"]) for r in results}


def test_search_finds_by_summary(android_graph: Graph) -> None:
    out = tools.kg_search(android_graph, "rank feed")
    assert out["count"] >= 1
    assert "feed_ranker" in _ids(out["results"])  # type: ignore[arg-type]


def test_search_kind_filter(android_graph: Graph) -> None:
    out = tools.kg_search(android_graph, "screen", kind="View")
    assert "bookmarks_screen" in _ids(out["results"])  # type: ignore[arg-type]
    assert "saved_article" not in _ids(out["results"])  # type: ignore[arg-type]


def test_node_full(android_graph: Graph) -> None:
    out = tools.kg_node(android_graph, "feed_ranker")
    assert out["found"] is True
    assert out["parity"] == "divergent"
    assert out["inbound_edges"] == []


def test_node_unknown_suggests(android_graph: Graph) -> None:
    out = tools.kg_node(android_graph, "feed")
    assert out["found"] is False
    assert "feed_ranker" in out["did_you_mean"]  # type: ignore[operator]


def test_neighborhood(android_graph: Graph) -> None:
    out = tools.kg_neighborhood(android_graph, "saved_article", depth=1)
    assert out["found"] is True
    ids = {str(n["id"]) for n in out["neighbors"]}  # type: ignore[union-attr]
    # inbound: bookmarks_screen, feed_ranker
    assert {"bookmarks_screen", "feed_ranker"} <= ids


def test_find_by_kind(android_graph: Graph) -> None:
    out = tools.kg_find_by_kind(android_graph, "domain")
    assert out["count"] == 1
    assert out["nodes"][0]["id"] == "feed_ranker"  # type: ignore[index]


def test_parity_gaps(android_graph: Graph) -> None:
    out = tools.kg_parity_gaps(android_graph)
    assert out["count"] == 2  # feed_ranker (divergent) + night_digest (android-only)
    assert out["by_status"] == {"divergent": 1, "android-only": 1}


def test_parity_gaps_status_filter(android_graph: Graph) -> None:
    out = tools.kg_parity_gaps(android_graph, status="only")
    assert _ids(out["gaps"]) == {"night_digest"}  # type: ignore[arg-type]
    out2 = tools.kg_parity_gaps(android_graph, status="divergent")
    assert _ids(out2["gaps"]) == {"feed_ranker"}  # type: ignore[arg-type]


def test_stats(android_graph: Graph) -> None:
    out = tools.kg_stats(android_graph)
    assert out["nodes"] == 4
    assert out["codebase"] == "android"
    assert out["parity"] == {"matched": 1, "divergent": 1, "android-only": 1}


def test_validate_android_with_source_and_peer(
    android_graph: Graph, ios_graph: Graph
) -> None:
    out = tools.kg_validate(android_graph, peer=ios_graph)
    # source resolved (fixtures/android/src exists)
    assert out["source_checked"] is True
    # one dangling edge: bookmarks_screen -> ghost_node
    assert out["dangling_edges"] == [{"node": "bookmarks_screen", "edge": "ghost_node"}]
    # one ungreppable anchor: night_digest theme symbol
    issues = out["anchor_issues"]
    assert any(i["node"] == "night_digest" for i in issues)  # type: ignore[union-attr]
    # matched/divergent counterparts are reciprocal with the peer -> no counterpart issues
    assert out["counterpart_issues"] == []
    # field consistency clean (android-only has no counterpart, divergent has both)
    assert out["field_issues"] == []
    assert out["ok"] is False  # because of the dangling edge + anchor issue


def test_validate_ios_flags_non_reciprocal(ios_graph: Graph, android_graph: Graph) -> None:
    out = tools.kg_validate(ios_graph, peer=android_graph)
    # bookmarks_view_model -> android#bookmarks_screen, which has no back-link
    msgs = [i["node"] for i in out["counterpart_issues"]]  # type: ignore[union-attr]
    assert "bookmarks_view_model" in msgs
    # ios source root (AcmeApp) doesn't exist in fixtures -> anchors unchecked
    assert out["source_checked"] is False


NONRECIP_FIELD_KG = """# X — Knowledge Graph

```
codebase: x
root: src
refreshed: 2026-01-01
```

## NODES

### N

| id | a |
| kind | Thing |
| anchors | `a.py#A` |
| summary | A. |
| edges | |
| parity | divergent |
| counterpart | ../y/KNOWLEDGE_GRAPH.md#b |
"""


def test_validate_field_issue_divergent_missing_divergence() -> None:
    g = parse_graph(NONRECIP_FIELD_KG, path="")
    out = tools.kg_validate(g)
    issues = [i["issue"] for i in out["field_issues"]]  # type: ignore[union-attr]
    assert any("divergence" in m for m in issues)

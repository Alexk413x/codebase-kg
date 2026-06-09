from __future__ import annotations

from codebase_kg.loader import parse_graph
from codebase_kg.models import Graph


def test_header_parsed(android_graph: Graph) -> None:
    h = android_graph.header
    assert h.codebase == "android"
    assert h.root == "src"
    assert h.counterpart == "../ios/KNOWLEDGE_GRAPH.md"
    assert h.language == "kotlin"
    assert h.refreshed == "2026-06-08"


def test_node_count_and_ids(android_graph: Graph) -> None:
    assert android_graph.ids == {
        "saved_article",
        "bookmarks_screen",
        "feed_ranker",
        "night_digest",
    }


def test_anchors_split_and_strip_backticks(android_graph: Graph) -> None:
    n = android_graph.by_id("saved_article")
    assert n is not None
    assert n.anchors == ["room/SavedArticleEntity.kt#SavedArticleEntity"]


def test_edges_parsed_including_dangling(android_graph: Graph) -> None:
    n = android_graph.by_id("bookmarks_screen")
    assert n is not None
    assert n.edges == ["saved_article", "ghost_node"]


def test_empty_edges_is_empty_list(android_graph: Graph) -> None:
    n = android_graph.by_id("night_digest")
    assert n is not None
    assert n.edges == []


def test_parity_fields(android_graph: Graph) -> None:
    fr = android_graph.by_id("feed_ranker")
    assert fr is not None
    assert fr.parity == "divergent"
    assert fr.counterpart == "../ios/KNOWLEDGE_GRAPH.md#personalized_ranking"
    assert fr.divergence is not None

    td = android_graph.by_id("night_digest")
    assert td is not None
    assert td.parity == "android-only"
    assert td.counterpart is None


def test_section_grouping(android_graph: Graph) -> None:
    n = android_graph.by_id("saved_article")
    assert n is not None
    assert n.section == "CORE DOMAIN MODEL"


def test_updated_field_parsed(android_graph: Graph) -> None:
    assert android_graph.by_id("saved_article") is not None
    assert android_graph.by_id("saved_article").updated == "2026-06-08"  # type: ignore[union-attr]
    assert android_graph.by_id("feed_ranker").updated == "2026-06-01"  # type: ignore[union-attr]
    assert android_graph.by_id("bookmarks_screen").updated is None  # type: ignore[union-attr]


def test_inbound_index(android_graph: Graph) -> None:
    # saved_article is referenced by bookmarks_screen and feed_ranker
    assert set(android_graph.inbound("saved_article")) == {"bookmarks_screen", "feed_ranker"}


LEGACY_KG = """# Legacy — Knowledge Graph

```
codebase: legacy
root: app
refreshed: 2026-01-01
```

## NODES

### CORE

| id | article |
| type | Model |
| files | `domain/Article.kt`, `domain/Source.kt` |
| details | The core article model. |
| deps | category |

| id | category |
| type | Enum |
| files | `domain/Category.kt` |
| details | Content categories. |
"""


PIPE_KG = """# P — Knowledge Graph

```
codebase: p
root: src
refreshed: 2026-01-01
```

## NODES

### N

| id | bookmarks_screen |
| type | View |
| files | `ui/BookmarksScreen.kt` |
| details | M3 tabs: Bookmarks | Read Later | Highlights. Swipe removes. |
| deps | BookmarksViewModel |
| parity | matched |
| counterpart | ../other/KNOWLEDGE_GRAPH.md#bookmarks_view |
"""


def test_value_with_internal_pipes_does_not_truncate_node() -> None:
    # Regression: a details/summary cell containing literal '|' must not cut the
    # node short and drop the trailing parity/counterpart rows.
    g = parse_graph(PIPE_KG, path="x")
    n = g.by_id("bookmarks_screen")
    assert n is not None
    assert "Read Later" in n.summary and "Highlights" in n.summary
    assert n.edges == ["BookmarksViewModel"]
    assert n.parity == "matched"
    assert n.counterpart == "../other/KNOWLEDGE_GRAPH.md#bookmarks_view"


def test_legacy_field_aliases() -> None:
    g = parse_graph(LEGACY_KG, path="x")
    a = g.by_id("article")
    assert a is not None
    assert a.kind == "Model"  # type -> kind
    assert a.anchors == ["domain/Article.kt", "domain/Source.kt"]  # files -> anchors
    assert a.summary == "The core article model."  # details -> summary
    assert a.edges == ["category"]  # deps -> edges

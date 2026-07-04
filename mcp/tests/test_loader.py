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


COMMENTED_KG = """# C — Knowledge Graph

```
codebase: c
root: src
refreshed: 2026-01-01
```

## NODES

### N

| id | feed_ranker   # the node |
| kind | Domain (pure)   # role |
| anchors | `domain/FeedRanker.kt#FeedRanker`   # main anchor |
| summary | Ranks the feed — see #ranking notes. |
| edges | saved_article   # dep |
| updated | 2026-06-01   # date this node was last verified vs source |
| parity | matched    # optional — multi-codebase only |
| counterpart | ../ios/KNOWLEDGE_GRAPH.md#personalized_ranking  # optional |
"""


def test_inline_comments_stripped_from_non_prose_fields() -> None:
    # Regression: template-style `  # comment` tails on node rows must not leak
    # into values; '#' glued to a path (anchor/counterpart) must survive.
    g = parse_graph(COMMENTED_KG, path="x")
    n = g.by_id("feed_ranker")
    assert n is not None
    assert n.kind == "Domain (pure)"
    assert n.anchors == ["domain/FeedRanker.kt#FeedRanker"]
    assert n.edges == ["saved_article"]
    assert n.updated == "2026-06-01"
    assert n.parity == "matched"
    assert n.counterpart == "../ios/KNOWLEDGE_GRAPH.md#personalized_ranking"
    # summary is free prose — a literal " #…" there is content, not a comment
    assert n.summary == "Ranks the feed — see #ranking notes."


SEPARATOR_KG = """# S — Knowledge Graph

```
codebase: s
root: src
refreshed: 2026-01-01
```

## NODES

### N

| id | article |
| :-- | ---: |
| kind | Model |
| --- | --- |
| anchors | `domain/Article.kt#Article` |
| summary | Core model. |
| edges | category |

| id | category |
| kind | Enum |
| anchors | `domain/Category.kt#Category` |
| summary | Content categories. |
"""


def test_table_separator_row_does_not_truncate_node() -> None:
    # Regression: a formatter-inserted `| --- | --- |` row must not flush the
    # node and truncate it to id-only.
    g = parse_graph(SEPARATOR_KG, path="x")
    n = g.by_id("article")
    assert n is not None
    assert n.kind == "Model"
    assert n.anchors == ["domain/Article.kt#Article"]
    assert n.edges == ["category"]


DUP_KG = """# D — Knowledge Graph

```
codebase: d
root: src
refreshed: 2026-01-01
```

## NODES

### N

| id | article |
| kind | Model |
| summary | First. |

| id | article |
| kind | Enum |
| summary | Second — same id. |
"""


def test_duplicate_ids_recorded() -> None:
    # SCHEMA.md §4: ids must be unique. The index keeps the last node, but the
    # collision is recorded for kg_validate to report.
    g = parse_graph(DUP_KG, path="x")
    assert len(g.nodes) == 2
    assert g.duplicate_ids == ["article"]
    n = g.by_id("article")
    assert n is not None and n.kind == "Enum"  # last wins the index

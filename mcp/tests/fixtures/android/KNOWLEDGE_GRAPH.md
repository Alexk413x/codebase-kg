# Acme Android — Knowledge Graph (FIXTURE)

```
codebase:    android
root:        src
counterpart: ../ios/KNOWLEDGE_GRAPH.md
language:    kotlin
refreshed:   2026-06-08
```

Tiny fixture KG for the codebase-kg loader/tools tests. Intentionally contains one
dangling edge (`ghost_node`) and one ungreppable anchor (`theme/Theme.kt#MissingSymbolXYZ`).

## NODES

### CORE DOMAIN MODEL

| id          | saved_article |
| kind        | Room @Entity |
| anchors     | `room/SavedArticleEntity.kt#SavedArticleEntity` |
| summary     | Persisted bookmark in the `saved_articles` table, indexed by articleId for dedup. |
| edges       | bookmarks_screen |
| parity      | matched |
| counterpart | ../ios/KNOWLEDGE_GRAPH.md#saved_article |

### VIEWS

| id          | bookmarks_screen |
| kind        | View (Composable) |
| anchors     | `ui/BookmarksScreen.kt#BookmarksScreen` |
| summary     | Bookmarks list screen; renders saved articles, swipe to delete. |
| edges       | saved_article, ghost_node |

### DOMAIN

| id          | feed_ranker |
| kind        | Domain (pure) |
| anchors     | `domain/FeedRanker.kt#FeedRanker` |
| summary     | Ranks the feed by freshness + per-source/category weight. Wired in the feed view model. |
| edges       | saved_article |
| parity      | divergent |
| counterpart | ../ios/KNOWLEDGE_GRAPH.md#personalized_ranking |
| divergence  | Android: pure, persisted, wired. iOS: actor, in-memory, unwired. |

### THEME

| id          | night_digest |
| kind        | Theme |
| anchors     | `theme/Theme.kt#MissingSymbolXYZ` |
| summary     | Time-windowed warm-amber theme (22:00–06:00). |
| edges       |  |
| parity      | android-only |

# App iOS — Knowledge Graph (FIXTURE)

```
codebase:    ios
root:        App
counterpart: ../android/KNOWLEDGE_GRAPH.md
language:    swift
refreshed:   2026-06-08
```

Tiny fixture KG. `bookmarks_view_model` deliberately links to a peer node that does NOT
link back, to exercise the non-reciprocal counterpart check.

## NODES

### DOMAIN

| id          | saved_article |
| kind        | @Model (SwiftData) |
| anchors     | `Models.swift#SavedArticle` |
| summary     | Persisted bookmark. SwiftData `@Model final class SavedArticle`. |
| edges       | bookmarks_view_model |
| parity      | matched |
| counterpart | ../android/KNOWLEDGE_GRAPH.md#saved_article |

### VIEW MODELS

| id          | bookmarks_view_model |
| kind        | ViewModel |
| anchors     | `BookmarksViewModel.swift#BookmarksViewModel` |
| summary     | Backs BookmarksView; loads and removes `SavedArticle`s from the model context. |
| edges       | saved_article |
| parity      | matched |
| counterpart | ../android/KNOWLEDGE_GRAPH.md#bookmarks_screen |

### SERVICES

| id          | personalized_ranking |
| kind        | actor (Service) |
| anchors     | `PersonalizedRankingService.swift#PersonalizedRankingService` |
| summary     | Actor scoring articles per-category with in-memory signals; defined but unwired. |
| edges       |  |
| parity      | divergent |
| counterpart | ../android/KNOWLEDGE_GRAPH.md#feed_ranker |
| divergence  | iOS: actor, per-category, in-memory, unwired. Android: pure, persisted, wired. |

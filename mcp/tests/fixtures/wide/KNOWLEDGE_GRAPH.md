# App Web — Knowledge Graph (FIXTURE, wide table form)

```
codebase:    web
root:        src
counterpart: ../ (none)
language:    typescript
refreshed:   2026-07-27
```

Fixture for the **wide** one-row-per-node table shape, which real graphs use
(Acme-iOS is written this way) and which the original vertical-only parser read
as a single node called `kind`. Also exercises a `(none)` counterpart
placeholder and a literal `|` inside a summary cell.

## NODES

### ROUTES

| id | kind | anchors | summary | edges | updated | parity |
|---|---|---|---|---|---|---|
| app-router | Router | `routes/Router.tsx#Router` | Top-level route table; lazy-loads each page shell. | home-page, saved-page | 2026-07-27 | web-only |
| home-page | Page | `pages/HomeView.tsx#HomeView` | Landing feed. Three sections: Top | Recent | For You. Fixed in ACME-901. | article-card | 2026-07-27 | web-only |

### COMPONENTS

| id | kind | anchors | summary | edges | updated | parity |
|---|---|---|---|---|---|---|
| saved-page | Page | `pages/SavedView.tsx#SavedView` | Bookmarks list; removes via the store. | article-card | 2026-07-27 | web-only |
| article-card | Component | `components/ArticleCard.tsx#ArticleCard` | Single article tile with a bookmark toggle. |  | 2026-07-27 | web-only |

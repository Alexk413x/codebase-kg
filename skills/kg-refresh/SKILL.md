---
name: kg-refresh
description: This skill should be used when the user asks to "refresh the code graph", "update the KG", "re-sync code_graph.db with the code", "the graph is stale", or after shipping a feature when the graph should reflect new/changed/deleted code. It comprehensively re-derives the affected nodes against current source and rebuilds the committed database. (For a from-scratch graph use kg-build; for a read-only drift report use kg-audit or kg-validate.)
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_find_by_path
  - mcp__codebase-kg__kg_upsert_node
  - mcp__codebase-kg__kg_delete_node
  - mcp__codebase-kg__kg_add_link
  - mcp__codebase-kg__kg_remove_link
  - Read
  - Grep
  - Glob
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git ls-files:*)
  - Bash(python -m codebase_kg.export:*)
  - Bash(python -m codebase_kg.build:*)
  - Write
  - Edit
---

# kg-refresh — re-derive the graph against current source

Update `knowledge/code_graph.db` so it mirrors the code as it is **now**. Every refresh updates
*all* affected **nodes** — add new, edit changed, remove deleted — plus edges.

## Two ways to write, and how to choose

Scope the change set (step 1) before picking one.

### One or two nodes → the write tools

`kg_upsert_node` for a description, an anchor list or an edge list; `kg_delete_node` for code that
is gone; `kg_add_link` / `kg_remove_link` for a pointer into another graph.

```
kg_upsert_node(nodes=[{"id": "feed_ranker",
                       "description": "Ranks the feed by freshness and per-source weight.",
                       "anchors": ["domain/FeedRanker.kt#FeedRanker"]}])
kg_delete_node(ids=["legacy_sync_worker"])                    # previews what cascades
kg_delete_node(ids=["legacy_sync_worker"], dry_run=False)
```

Only the keys you supply change, so omitting `edges` keeps them. `anchors`, `edges` and
`external_links` **replace** the whole list when present — read the node first if you mean to
append. Each call is atomic, runs `kg_validate` against the result and refuses anything that
introduces a new finding, and hands back every field it changed. A rejected call leaves the
committed file byte-identical, and there is no scratch file to clean up.

### A whole change set → export → edit → build

The usual case for a refresh: several nodes move together, and reading the JSON diff before it lands
is the point.

```sh
python -m codebase_kg.export -o .kg-export.json          # current graph, as JSON
#   … edit .kg-export.json …
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
```

Two properties make this safe:

- **Lossless.** Building an unedited export reproduces the file byte for byte, so an unchanged
  refresh leaves the git diff empty. Any change in the committed file is a change you made.
- **Validated.** The builder refuses a document that breaks a rule, names the node, and writes
  nothing. A dangling edge or a description carrying a ticket ref cannot land.

Delete `.kg-export.json` when you are done — it is a working file, not an artifact.

**Do not interleave the two in one refresh.** An export is a snapshot; building one taken before a
write-tool call silently reverts that call, and the result looks like a clean rebuild.

## Workflow

### 1. Scope the change set
Determine what changed since the graph was last built:

- `git diff --name-only <since>...HEAD`, where `<since>` is a commit near the graph's `generated`
  date (`kg_stats` reports it), or `git log --since=<generated>`. If unsure, scope to the user's
  named feature.
- Map changed files → owning nodes with **`kg_find_by_path`** (that is what it is for), falling back
  to `kg_search` for concepts.

### 2. Re-derive each affected node
For every changed file / feature, **read the current source** and reconcile its node:

- **Added** files/units → **add** a node (schema shape; grep-confirmed `path#Symbol` anchors).
- **Changed** units → **edit** the node: fix `anchors` (symbols added/renamed/removed), rewrite the
  `description` to current behavior, update `edges`.
- **Deleted/renamed** units → **remove** or **rename** the node and fix every `edges` entry that
  pointed at it. (The builder will refuse the document otherwise, which is the safety net, not the
  plan.)

Keep descriptions inside the contract (`SCHEMA.md` §5): one short line, ≤ 240 chars, present tense,
no ticket ids, no dates, no "now does X instead of Y". If you find yourself narrating the change you
just made, that sentence belongs in the commit message.

### 3. Set `generated`
Set the document's `generated` to today. It is provenance — it records when the artifact was built.
Nothing gates on it, so it is not a substitute for doing step 2.

### 4. Build and validate
Run the builder. Then `kg_validate` — fix ungreppable anchors, and add nodes for anything under
`coverage.gaps` that deserves one (or add it to `exempt`, which records the decision instead of
leaving it looking like an oversight). Run `kg_stats` and report the delta.

For every node you actually re-read against source, the rebuild re-baselines it. Nodes you did not
touch keep their old baseline and keep appearing in `changed_since_built` — that is deliberate.
Do **not** reach for `--rebaseline` to silence it; that asserts you checked everything, and using it
to clear a report you did not act on destroys the only signal that distinguishes a verified
description from a plausible one.

### 5. Commit
Commit `knowledge/code_graph.db` alongside the source change.

## Rules

- **The nodes are the deliverable.** Bumping `generated` without touching nodes is exactly the drift
  this plugin exists to prevent — and now it is also pointless, since nothing reads the date as a
  freshness claim.
- If the change set is large, cover it fully or state explicitly which nodes were updated and which
  remain — never imply completeness that wasn't achieved.
- Claims are **source-derived**: read the file, don't infer from the commit message or ticket.
- Keep it **point-don't-copy**: anchors and descriptions, never pasted code.

## When parity exists

If nodes carry `parity`/`counterpart`, a code change may have **closed or opened a gap** (a feature
that was `<codebase>-only` now exists on both sides, or a `matched` pair diverged). Update the
parity fields on the affected nodes, or hand the cross-codebase reconciliation to `kg-link` if it
needs reading the peer repo. Note any parity change in the report.

Remember the store only accepts the three legal shapes (`SCHEMA.md` §9): `matched` needs a
counterpart, `divergent` needs a counterpart *and* a divergence line, `<codebase>-only` must have
neither.

## Report format

```
## Code graph refresh — <codebase> (generated → <today>)
Scope: <feature / commit range>
Nodes: +<added> / ~<edited> / -<removed>
Edges updated: <list>
Parity changes: <node-id: old → new, or none>
Validation: <clean | findings>
```

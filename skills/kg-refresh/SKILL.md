---
name: kg-refresh
description: This skill should be used when the user asks to "refresh the knowledge graph", "update the KG", "re-sync KNOWLEDGE_GRAPH.md with the code", "the KG is stale", or after shipping a feature when the KG should reflect new/changed/deleted code. It comprehensively re-derives the affected nodes against current source — never a header-or-date-only edit.
when_to_use: Use to bring an existing KG back in sync with current source after code changes. For a from-scratch KG use kg-build; for a read-only drift report use kg-audit or kg-validate.
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_node
  - Read
  - Grep
  - Glob
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git ls-files:*)
  - Write
  - Edit
---

# kg-refresh — re-derive the KG against current source

Update an existing `KNOWLEDGE_GRAPH.md` so it mirrors the code as it is **now**. The cardinal rule
(SCHEMA.md §6): **a header-or-date-only edit is forbidden.** Every refresh updates *all* affected
**nodes** — add new, edit changed, remove deleted — plus the edges and flows. Bumping `refreshed:`
without touching nodes is exactly the drift this plugin exists to prevent.

## Workflow

### 1. Scope the change set
Determine what changed since the KG was last refreshed:
- `git diff --name-only <since>...HEAD` where `<since>` is a commit near the KG's `refreshed:` date,
  or `git log --since=<refreshed>` to see the commits. If unsure, scope to the user's named feature.
- Map changed files → the nodes that anchor them (`kg_search` / `kg_node` to find owning nodes).

### 2. Re-derive each affected node
For every changed file / feature, **read the current source** and reconcile its node:
- **Added** files/units → **add** a node (schema shape; grep-confirmed `path#Symbol` anchors).
- **Changed** units → **edit** the node: fix `anchors` (symbols added/renamed/removed), rewrite the
  `summary` to current behavior, update `edges`, update any version/migration/wiring claims.
- **Deleted/renamed** units → **remove** or **rename** the node and fix every `edges` entry that
  pointed at it (no dangling edges).
- For every node you **add or edit**, set its `updated` to today. **Leave untouched nodes'
  `updated` unchanged** — that lag is the per-node staleness signal (`kg_stats` reports
  `updated.stale_vs_refreshed`; `kg-audit` can prioritize the oldest-`updated` nodes).

### 3. Update edges + flows
Refresh the `## EDGES` flows and the `## FEATURE → CODE MAP` for any path that changed. A new
top-level component (screen, view-model, service, module, route) gets a node **and** an edge to its
dependencies.

### 4. Bump and validate
Set `refreshed:` to today. Run `kg_validate` — fix dangling edges and ungreppable anchors. Run
`kg_stats` and report the delta (nodes added/edited/removed).

## The anti-drift contract (enforce it)

- Do **not** stop after editing the header narrative. The node tables are the deliverable.
- If the change set is large, cover it fully or state explicitly which nodes were updated and which
  remain to do — never imply completeness that wasn't achieved.
- Claims are **source-derived**: read the file, don't infer from the commit message or ticket.
- Keep it **point-don't-copy**: anchors and summaries, never pasted code.

## When parity exists

If nodes carry `parity`/`counterpart`, a code change may have **closed or opened a gap** (e.g. a
feature that was `<codebase>-only` now exists on both sides, or a `matched` pair diverged). Update
the parity fields on the affected nodes, or hand the cross-codebase reconciliation to `kg-link` if
it needs reading the peer repo. Note any parity change in the report.

## Report format

```
## KG refresh — <codebase> (refreshed → <today>)
Scope: <feature / commit range>
Nodes: +<added> / ~<edited> / -<removed>
Edges/flows updated: <list>
Parity changes: <node-id: old → new, or none>
Validation: <clean | findings>
```

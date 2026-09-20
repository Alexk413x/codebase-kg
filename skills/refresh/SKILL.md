---
name: refresh
description: This skill should be used when the user asks to "refresh the code graph", "update the KG", "re-sync code_graph.db with the code", "the graph is stale", after shipping a feature when the graph should reflect new/changed/deleted code, or after a codebase-kg pre-commit/pre-push staleness message naming unmapped, deleted-but-anchored, or digest-drifted files. It comprehensively re-derives the affected nodes against current source and rebuilds the committed database. This skill WRITES. (For a from-scratch graph use build; for a read-only report of what is stale without changing anything, use audit or validate.)
allowed-tools:
  # Both names the host gives the server: bare when the MCP server is installed
  # directly, prefixed when it arrives as a plugin.
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_find_by_path
  - mcp__codebase-kg__kg_upsert_node
  - mcp__codebase-kg__kg_delete_node
  - mcp__codebase-kg__kg_add_link
  - mcp__codebase-kg__kg_remove_link
  - mcp__codebase-kg__kg_add_reference
  - mcp__codebase-kg__kg_remove_reference
  - mcp__codebase-kg__kg_neighborhood
  - mcp__plugin_codebase-kg_codebase-kg__kg_validate
  - mcp__plugin_codebase-kg_codebase-kg__kg_stats
  - mcp__plugin_codebase-kg_codebase-kg__kg_search
  - mcp__plugin_codebase-kg_codebase-kg__kg_node
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_path
  - mcp__plugin_codebase-kg_codebase-kg__kg_upsert_node
  - mcp__plugin_codebase-kg_codebase-kg__kg_delete_node
  - mcp__plugin_codebase-kg_codebase-kg__kg_add_link
  - mcp__plugin_codebase-kg_codebase-kg__kg_remove_link
  - mcp__plugin_codebase-kg_codebase-kg__kg_add_reference
  - mcp__plugin_codebase-kg_codebase-kg__kg_remove_reference
  - mcp__plugin_codebase-kg_codebase-kg__kg_neighborhood
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
  - Bash(rm:*)
  # The runnable forms outside the plugin's own checkout. kg_stats reports
  # which one applies; `python -m` only works where the package imports.
  - Bash(uvx:*)
  - Bash(codebase-kg-build:*)
  - Bash(codebase-kg-export:*)
---

# refresh — re-derive the graph against current source

Update `knowledge/code_graph.db` so it mirrors the code as it is **now**. Every refresh updates
*all* affected **nodes** — add new, edit changed, remove deleted — plus edges.

> **Before running any CLI below, call `kg_stats` and read its `cli` field.** It reports the
> invocation that works *in this repo* — `uvx --from "<plugin>/mcp" codebase-kg-build …` when the
> plugin ships as a source checkout, or the bare `codebase-kg-build` when the package is installed.
> The `python -m codebase_kg.…` form written below is the plugin's own-checkout form; in a target
> repo that has the plugin but no importable `codebase_kg` it is a `ModuleNotFoundError`, and
> `CLAUDE_PLUGIN_ROOT` is not set in your shell so you cannot construct the path yourself.

## Two ways to write, and how to choose

Scope the change set (step 1) before picking one.

### One or two nodes → the write tools

`kg_upsert_node` for a description, an anchor list or an edge list; `kg_delete_node` for code that
is gone; `kg_add_link` / `kg_remove_link` for a pointer into another graph;
`kg_add_reference` / `kg_remove_reference` for the documentation a node depends on.

```
kg_upsert_node(nodes=[{"id": "feed_ranker",
                       "description": "Ranks the feed by freshness and per-source weight.",
                       "anchors": ["domain/FeedRanker.kt#FeedRanker"]}])
kg_delete_node(ids=["legacy_sync_worker"])                    # previews what cascades
kg_delete_node(ids=["legacy_sync_worker"], dry_run=False)
```

Only the keys you supply change, so omitting `edges` keeps them. `anchors`, `edges`,
`external_links` and `references` **replace** the whole list when present — read the node first
if you mean to append. A reference's `path` / `symbol` must equal one of the node's anchors, so
when an anchor moves, move the references that narrow to it in the same call. Each call is atomic, runs `kg_validate` against the result and refuses anything that
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

- **If a pre-commit or pre-push staleness message brought you here, start from its three lists.**
  They are already scoped and categorized: source no node covers (`+`), deleted files the graph still
  anchors on (`-`), and mapped files whose contents no longer match the digest recorded at build time
  (`~` — the anchor still resolves, the description may not; `SCHEMA.md` §6.3). Each list caps at 15
  entries, so re-derive with `git diff` only when one says "and N more".
- Otherwise, **diff from the commit that last touched the graph itself**. That commit is the
  watermark: everything after it is, by definition, code the graph has not seen.

  ```sh
  SINCE=$(git log -1 --format=%H -- knowledge/code_graph.db)
  git diff --name-only "$SINCE"...HEAD
  ```

  Use this rather than the `generated` date. A date needs a "commit near it" — a judgement call at
  the one point in the workflow where the scope has to be exact — and `SCHEMA.md` §7 is explicit that
  the date is provenance, never a freshness claim: it can be bumped without a node changing, and a
  refresh scoped from a bumped date silently covers nothing. The graph's own commit cannot be wrong
  about when the graph last moved.

  Three cases the one-liner does not cover, each with an honest answer rather than a guess:

  | Situation | Scope |
  |---|---|
  | `$SINCE` is empty — the graph is new or uncommitted | The whole repo. There is no watermark yet, and that is `/codebase-kg:build`'s job, not a diff. |
  | `$SINCE` is not an ancestor of `HEAD` (rebased, or a branch) | `git merge-base $SINCE HEAD`, so the range is what this branch added rather than what it diverged around. |
  | The working tree is dirty | Add `git status --porcelain` — uncommitted work is exactly what the graph has not seen, and it is what the pre-commit hook will name next. |

  If unsure, or when the user named a feature, scope to that instead and say so.
- Map changed files → owning nodes with **`kg_find_by_path`** (that is what it is for), falling back
  to `kg_search` for concepts.

### 2. Re-derive each affected node
For every changed file / feature, **read the current source** and reconcile its node:

- **Unmapped** files/units — newly added, or long-present and never mapped → **add** a node (schema
  shape; grep-confirmed `path#Symbol` anchors). Not only the newly added ones: a file created ten
  commits ago and never covered is reported by the hooks and is exactly as much of a hole.
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
parity fields on the affected nodes, or hand the cross-codebase reconciliation to `link` if it
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

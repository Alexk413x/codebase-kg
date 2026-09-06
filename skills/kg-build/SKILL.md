---
name: kg-build
description: This skill should be used when the user asks to "build a code graph", "build a knowledge graph", "bootstrap a KG", "create a code_graph.db", "map this codebase", "generate the code graph", or onboard a repo that has no graph yet (or whose graph is too narrow to keep). It reads the source tree and emits a source-derived, symbol-anchored, committed code_graph.db per the codebase-kg schema. (For updating an existing graph against changed source, use kg-refresh instead. For converting an old KNOWLEDGE_GRAPH.md, run python -m codebase_kg.migrate — do not rebuild from scratch.)
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Bash(git diff:*)
  - Bash(python -m codebase_kg.build:*)
  - Bash(python -m codebase_kg.export:*)
  - Task
  - Write
  - Edit
---

# kg-build — bootstrap a code graph from source

Read a repo's source tree and emit a committed `knowledge/code_graph.db` that conforms to the
**schema** (`SCHEMA.md` at the plugin root). The graph is **source-derived** (every claim traces to
code), **symbol-anchored** (`path#Symbol`, never line numbers), and **point-don't-copy** (reference
symbols; never paste code). It is **descriptive**, not prescriptive.

> Read `SCHEMA.md` before starting — it is the contract this skill writes to. For the parallel
> sub-agent partitioning strategy on large repos, read `references/build-strategy.md`.

**If the repo already has a `knowledge/KNOWLEDGE_GRAPH.md`, stop and migrate instead:**
`python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md`. Converting preserves curated
structure; rebuilding throws it away.

## How the graph is written

A bootstrap is bulk work by definition — every node at once — so this skill always uses the build
path. The artifact is SQLite, so it is not written with an editor. You author a **JSON document**
and hand it to the builder:

```sh
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json
```

The builder validates before writing anything. If a node breaks a rule it names that node and writes
nothing — fix that node and run again. Delete the JSON when you are done; `code_graph.db` is the
committed artifact.

The MCP write tools (`kg_upsert_node` and friends) are for **targeted** corrections to a graph that
already exists — one description, one link. Do not build a graph a node at a time with them: a
bootstrap authored that way runs the whole-graph validator once per node and produces no reviewable
diff of the result.

```json
{
  "codebase": "android",
  "root": "app/src/main/java/com/example/acme",
  "language": "kotlin",
  "counterpart": "../../acme-ios/knowledge/code_graph.db",
  "generated": "2026-07-30",
  "nodes": [
    {
      "id": "feed_ranker",
      "kind": "Domain (pure)",
      "description": "Ranks the main feed by freshness, breaking flag and normalized source/category weight.",
      "section": "DOMAIN",
      "anchors": ["domain/FeedRanker.kt#FeedRanker"],
      "edges": ["feed_view_model", "reading_event_entity"]
    }
  ]
}
```

## Workflow

### 1. Configure
Read `.claude/codebase-kg.local.md` if present (`codebase`, `root`, `graph_path`, `counterpart`,
`language`). If absent, infer: `root` = the main source dir, `codebase` = a short name. The graph is
**always** written to `knowledge/code_graph.db` — create `knowledge/` if needed. There is no
repo-root fallback; only a `graph_path` override in `.local.md` changes the location. Confirm the
inferred config with the user in one message before building.

### 2. Survey the source tree
`git ls-files` (or `Glob`) under `root` to inventory files. Group them into **subsystems** — the
natural sections (entry point, navigation/shell, domain model, view-models, views, services, data
layer, DI/modules, theme, …). The grouping fits *this* codebase; do not impose another repo's
sections.

### 3. Derive nodes (parallelize on large repos)
For each subsystem, identify the meaningful units (a screen, a model, a service, a module). For
each, write a node per `SCHEMA.md` §4:

- `id` — a stable concept slug (not the filename).
- `kind` — free text (`Composable`, `ViewModel`, `Service`, `@Model`, `module`, …).
- `anchors` — `path#Symbol` for each defining symbol, relative to `root`. **Grep the symbol to
  confirm it exists** before writing the anchor. Never write a line number.
- `description` — **one short line, ≤ 240 chars**: what it is and what it does, now. No ticket ids,
  no dates, no change narrative — see §5 of `SCHEMA.md`. The builder rejects all three.
- `edges` — ids of nodes it depends on / relates to (intra-graph only). Every id must exist, or the
  build fails.
- `section` — the subsystem grouping.

On a repo big enough that one pass would be shallow, fan out one `Task` sub-agent per subsystem
(read-only Explore agents) that returns nodes in the JSON node shape; then merge into one document.
See `references/build-strategy.md`.

### 4. Build
Write the JSON, then run the builder. Iterate on any node it names until it writes cleanly.

### 5. Validate
Run `kg_validate` (or the `kg-validate` skill) and fix what it finds — chiefly ungreppable anchors
and `coverage.gaps`. Run `kg_stats` and report the shape (node/edge counts, kinds) to the user.

If `coverage.declared` is `false`, go back to step 2 and write `covers`. A graph that has not
declared its scope cannot report a file type it never covered, so a clean result at this point is
not evidence of anything.

### 6. Commit
Add `knowledge/code_graph.db binary diff=codegraph` to the repo's `.gitattributes`, then commit the
graph. It is a committed artifact — that is the point of it. The `diff=codegraph` half makes the
file reviewable once a clone runs `/codebase-kg:setup`; committing the attribute means every
clone gets the wiring even though the driver itself is local config.

### 7. Parity (only if paired)
If a `counterpart` repo exists, leave parity fields out of this pass and hand off to `kg-link`,
which reads both codebases. Building one side cleanly first is the right order.

## Quality bar

- **Every anchor greps.** If a symbol can't be found, the node is wrong — fix it, don't ship it.
- **Descriptions state the present.** If you catch yourself writing why something changed, that
  belongs in the commit, not the graph.
- **No copied code, no line numbers, no ticket-derived claims.**
- **Comprehensive, not partial.** Cover the subsystems; a half-built graph that looks complete is
  worse than none. If scope is limited, say which subsystems were covered and which were not.

## Resources

- **`references/build-strategy.md`** — partitioning a large repo across parallel sub-agents, and the
  node-derivation checklist per subsystem.
- **`SCHEMA.md`** (plugin root) — the node/edge/parity contract.

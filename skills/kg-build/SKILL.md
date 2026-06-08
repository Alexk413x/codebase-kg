---
name: kg-build
description: This skill should be used when the user asks to "build a knowledge graph", "bootstrap a KG", "create a KNOWLEDGE_GRAPH.md", "map this codebase", "generate the code graph", or onboard a repo that has no KG yet. It reads the source tree and emits a source-derived, symbol-anchored KNOWLEDGE_GRAPH.md per the codebase-kg schema.
when_to_use: Use to create a KG from scratch for a repo that does not have one (or whose KG is too narrow to keep). For updating an existing KG against changed source, use kg-refresh instead.
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Bash(git diff:*)
  - Task
  - Write
  - Edit
---

# kg-build — bootstrap a knowledge graph from source

Read a repo's source tree and emit a `KNOWLEDGE_GRAPH.md` that conforms to the **schema**
(`SCHEMA.md` at the plugin root). The KG is **source-derived** (every claim traces to code),
**symbol-anchored** (`path#Symbol`, never line numbers), and **point-don't-copy** (reference
symbols; never paste code). It is **descriptive**, not prescriptive.

> Read `SCHEMA.md` before starting — it is the contract this skill writes to. For the parallel
> sub-agent partitioning strategy on large repos, read `references/build-strategy.md`.

## Workflow

### 1. Configure
Read `.claude/codebase-kg.local.md` if present (`codebase`, `root`, `kg_path`, `counterpart`,
`language`). If absent, infer: `root` = the main source dir, `codebase` = a short name, `kg_path` =
`KNOWLEDGE_GRAPH.md` at repo root (or `knowledge/` if that convention exists). Confirm the inferred
config with the user in one message before writing.

### 2. Survey the source tree
`git ls-files` (or `Glob`) under `root` to inventory files. Group them into **subsystems** — the
natural `###` sections (entry point, navigation/shell, domain model, view-models, views, services,
data layer, DI/modules, theme, …). The grouping fits *this* codebase; do not impose another repo's
sections.

### 3. Derive nodes (parallelize on large repos)
For each subsystem, identify the meaningful units (a screen, a model, a service, a module). For
each, write a node per `SCHEMA.md` §4:
- `id` — a stable concept slug (not the filename).
- `kind` — free text (`Composable`, `ViewModel`, `Service`, `@Model`, `module`, …).
- `anchors` — `path#Symbol` for each defining symbol. **Grep the symbol to confirm it exists** before
  writing the anchor. Never write a line number.
- `summary` — what it is/does in this codebase, pointer-dense, no copied code.
- `edges` — ids of the nodes it depends on / relates to (intra-KG only).

On a repo big enough that one pass would be shallow, fan out one `Task` sub-agent per subsystem
(read-only Explore agents) that returns nodes in schema shape; then assemble. See
`references/build-strategy.md`.

### 4. Assemble the document
Write `kg_path` using `templates/KNOWLEDGE_GRAPH.template.md` as the skeleton:
- Header block (`codebase`, `root`, `counterpart?`, `language?`, `refreshed` = today).
- The **update policy** paragraph, copied verbatim (SCHEMA.md §6) — every KG carries it.
- `## NODES` grouped into `###` sections.
- `## EDGES` — narrate the multi-hop flows (navigation, data, notification, DI) the node `edges`
  can't show linearly.
- Optional `## FEATURE → CODE MAP`, `## KEY DECISIONS`, `## ARCHITECTURE SUMMARY` once the graph is
  large enough to warrant a search index and cold-start orientation.

### 5. Validate
Run `kg_validate` (or the `kg-validate` skill). Fix dangling edges and ungreppable anchors before
finishing. Run `kg_stats` and report the shape (node/edge counts, kinds) to the user.

### 6. Parity (only if paired)
If a `counterpart` repo exists, leave parity fields out of this pass and hand off to `kg-link`,
which reads both codebases. Building one side cleanly first is the right order.

## Quality bar

- **Every anchor greps.** If a symbol can't be found, the node is wrong — fix it, don't ship it.
- **No copied code, no line numbers, no ticket-derived claims.**
- **Comprehensive, not partial.** Cover the subsystems; a half-built KG that looks complete is worse
  than none. If scope is limited by budget, say which subsystems were covered and which were not.

## Resources

- **`references/build-strategy.md`** — partitioning a large repo across parallel sub-agents, and the
  node-derivation checklist per subsystem.
- **`SCHEMA.md`** (plugin root) — the node/edge/parity contract.
- **`templates/KNOWLEDGE_GRAPH.template.md`** — the document skeleton.

---
name: kg-audit
description: >-
  This skill should be used when the user asks to "audit the code graph", "check the KG against the source", "is the graph accurate", "find stale or inaccurate nodes", or wants a source-vs-graph verification sweep. It is the deep, SEMANTIC, multi-agent sweep: it partitions the graph, verifies each node's anchors and claims against current source, and reports STALE / MISSING / INACCURATE — advisory output, no edits. (For the fast, deterministic check, use kg-validate instead; to actually fix what the audit finds, use kg-refresh.)
allowed-tools:
  - mcp__codebase-kg__kg_stats
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_find_by_kind
  - mcp__codebase-kg__kg_find_by_path
  - mcp__codebase-kg__kg_validate
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Bash(python -m codebase_kg.export:*)
  - Task
---

# kg-audit — source-vs-graph accuracy sweep (advisory)

Verify that an existing `knowledge/code_graph.db` still tells the truth about the code. This is the
**deep, read-only** check — it reads source and compares it to every node's claims. It **reports**;
it does not edit (hand fixes to `kg-refresh`). Advisory, never blocking.

> For the multi-agent partitioning recipe and the per-node verification checklist, read
> `references/audit-pattern.md`.

## What it catches that kg-validate can't

`kg-validate` is deterministic: does this anchor resolve, is this file covered, does the peer link
back. `kg-audit` is **semantic** — it reads the source and asks whether the `description` is still
*true*: dead class names, renamed symbols, wiring that changed, a node describing behavior the code
no longer has, claims that were never accurate.

It also catches the failure `kg-validate` structurally cannot: a node whose anchors all resolve
perfectly but whose description describes the wrong thing.

## Getting the nodes

Use `python -m codebase_kg.export -o .kg-export.json` for the whole graph as JSON, or `kg_find_by_kind` /
`kg_node` to pull the slice under audit. Export is usually right here — the audit reads every node
anyway, and the JSON is easy to partition across sub-agents.

## Workflow

### 1. Inventory
Run `kg_stats` for node count and sections. Export the graph. Partition the nodes into ~4 balanced
groups (by section, so each group is coherent).

### 2. Verify each group (parallelize)
Fan out one `Task` sub-agent per group (read-only). Each agent, for every node in its group:

- Resolves each `path#Symbol` anchor in source (grep). Symbol gone → **STALE**.
- Reads the anchored source and compares it to the `description` / `edges` / `kind`. Mismatch →
  **INACCURATE** (name the specific wrong claim).
- Notes source units in the subsystem that have **no node** → **MISSING**.

See `references/audit-pattern.md` for the exact agent contract.

### 3. Reverse pass (MISSING)
`kg_validate` already reports `coverage.gaps` — files `covers` says should be mapped and aren't.
Start there; it is exact and free. If `coverage.declared` is `false` the graph has no `covers`, so
that list only sees file types already anchored — say so, and treat declaring `covers` as the first
MISSING finding, because until it exists an entire category can be absent without registering. Then use judgement about which of them deserve a node (a
one-line extension file may not; a new service does). Supplement with `git ls-files` for units that
*are* anchored but whose containing feature is under-modeled.

### 4. Consolidate + report
Merge agent findings, dedup, sort by severity. Fold in `kg_validate`'s findings. Report; recommend
`kg-refresh` to fix.

## Report format

```
## Code graph audit — <codebase> (generated <date>, <N> nodes)
Verified <N> nodes across <M> groups.

STALE (anchor/symbol no longer in source) — <n>:
- <node-id>: `<path#Symbol>` — <symbol gone | file moved>

INACCURATE (claim contradicts source) — <n>:
- <node-id>: <the specific wrong claim> → <what source actually shows>

MISSING (source unit with no node) — <n>:
- <path#Symbol> (<kind>) — not represented in the graph

Structural (from kg_validate): <summary>

Verdict: <accurate | N findings — run kg-refresh to reconcile>
```

## Rules

- **Source is ground truth.** When the graph and the code disagree, the code wins and the graph is
  the finding.
- **Read-only.** Do not edit the graph here. The audit's value is an honest, complete findings list.
- **No silent passes.** If a node couldn't be verified (source not reachable), say so — don't mark
  it accurate.
- **Don't audit for history.** A description saying nothing about *why* the code is the way it is
  correct behavior, not a gap. Ticket refs, dates and change narrative are excluded by design
  (`SCHEMA.md` §5); flagging their absence is a misreading of the schema.

## Resources

- **`references/audit-pattern.md`** — the 4-agent partition recipe, sub-agent contract, and per-node
  verification checklist.

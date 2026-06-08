---
name: kg-audit
description: This skill should be used when the user asks to "audit the knowledge graph", "check the KG against the source", "is the KG accurate", "find stale or inaccurate nodes", or wants a source-vs-KG verification sweep. It is the deep, SEMANTIC, multi-agent sweep: it partitions the KG, verifies each node's anchors and claims against current source, and reports STALE / MISSING / INACCURATE — advisory output, no edits. (For the fast, deterministic STRUCTURAL check, use kg-validate instead.)
when_to_use: Use for a deep, read-only accuracy check of an existing KG against the codebase (the proven multi-agent verification sweep). For the cheap deterministic structural check use kg-validate; to actually fix what the audit finds use kg-refresh.
allowed-tools:
  - mcp__codebase-kg__kg_stats
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_validate
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Task
---

# kg-audit — source-vs-KG accuracy sweep (advisory)

Verify that an existing `KNOWLEDGE_GRAPH.md` still tells the truth about the code. This is the
**deep, read-only** check — it reads source and compares it to every node's claims. It **reports**;
it does not edit (hand fixes to `kg-refresh`). Advisory, never blocking.

> For the multi-agent partitioning recipe and the per-node verification checklist, read
> `references/audit-pattern.md`.

## What it catches that kg-validate can't

`kg-validate` is deterministic and structural (dangling edges, ungreppable symbols, parity field
shape). `kg-audit` is **semantic**: it reads the source and checks whether the `summary` is still
*true* — wrong version numbers, dead class names, renamed symbols, undocumented features, wiring
that changed, claims that were never accurate. (This is the sweep that catches multi-sprint node
drift — stale dependency/migration versions, dead class names, undocumented features — that
accumulates while only the KG header gets bumped.)

## Workflow

### 1. Inventory
Run `kg_stats` for the node count and sections. Read the KG. Partition the nodes into ~4 balanced
groups (by section, so each group is coherent).

### 2. Verify each group (parallelize)
Fan out one `Task` sub-agent per group (read-only). Each agent, for every node in its group:
- Resolves each `anchors` `path#Symbol` in source (grep). Symbol gone → **STALE**.
- Reads the anchored source and compares it to the `summary`/`edges`/`kind`. Mismatch → **INACCURATE**
  (name the specific wrong claim: version, symbol, wiring, count).
- Notes source units in the subsystem that have **no node** → **MISSING**.
Each agent returns findings in the table format below. See `references/audit-pattern.md` for the
exact agent contract.

### 3. Reverse pass (MISSING)
Independently, `git ls-files` under `root` and spot meaningful units (new screens, services,
modules, migrations) with no corresponding node. These are the dangerous gaps — a KG that looks
complete but silently omits features.

### 4. Consolidate + report
Merge agent findings, dedup, sort by severity. Run `kg_validate` too and fold its structural
findings in. Report; recommend `kg-refresh` to fix.

## Report format

```
## KG audit — <codebase> (refreshed <date>, <N> nodes)
Verified <N> nodes across <M> groups.

STALE (anchor/symbol no longer in source) — <n>:
- <node-id>: `<path#Symbol>` — <symbol gone | file moved>

INACCURATE (claim contradicts source) — <n>:
- <node-id>: <the specific wrong claim> → <what source actually shows>

MISSING (source unit with no node) — <n>:
- <path#Symbol> (<kind>) — not represented in the KG

Structural (from kg_validate): <summary>

Verdict: <accurate | N findings — run kg-refresh to reconcile>
```

## Rules

- **Source is ground truth.** When the KG and the code disagree, the code wins and the KG is the
  finding.
- **Read-only.** Do not edit the KG here. The audit's value is an honest, complete findings list.
- **No silent passes.** If a node couldn't be verified (source not reachable), say so — don't mark
  it accurate.

## Resources

- **`references/audit-pattern.md`** — the 4-agent partition recipe, sub-agent contract, and per-node
  verification checklist.

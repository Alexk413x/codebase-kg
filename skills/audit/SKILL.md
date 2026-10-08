---
name: audit
description: >-
  Deep, multi-agent accuracy sweep of a code graph. It partitions the graph, checks each node's anchors and claims against current source, and reports STALE / MISSING / INACCURATE nodes without editing anything. Use when the user asks to "audit the code graph", "is the graph telling the truth", "are the descriptions still accurate" or "find stale or inaccurate nodes". (For the fast deterministic check use validate; to fix what the audit finds, use refresh.)
allowed-tools:
  # Both names the host gives the server: bare when the MCP server is installed
  # directly, prefixed when it arrives as a plugin.
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_find_by_kind
  - mcp__codebase-kg__kg_find_by_path
  - mcp__codebase-kg__kg_neighborhood
  - mcp__plugin_codebase-kg_codebase-kg__kg_node
  - mcp__plugin_codebase-kg_codebase-kg__kg_search
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_kind
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_path
  - mcp__plugin_codebase-kg_codebase-kg__kg_neighborhood
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Agent
  - Bash(rm .kg-export.json)
  - Bash(uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" *)
---

# audit — source-vs-graph accuracy sweep (advisory)

Verify that an existing `knowledge/code_graph.db` still tells the truth about the code. This is the
**deep, read-only** check — it reads source and compares it to every node's claims. It **reports**;
it does not edit (hand fixes to `refresh`). Advisory, never blocking.

> For the multi-agent partitioning recipe and the per-node verification checklist, read
> `references/audit-pattern.md`.


> **Run the graph CLIs through the Bash tool with the plugin's runner.** It needs only `uv` on
> `PATH`, works from any repo, and builds no environment. The same runner takes `build`,
> `export`, `migrate` and `upgrade`:
>
> ```sh
> uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" export -o .kg-export.json
> ```

## What it catches that validate can't

`validate` is deterministic: does this anchor resolve, is this file covered, does the peer link
back. `audit` is **semantic** — it reads the source and asks whether the `description` is still
*true*: dead class names, renamed symbols, wiring that changed, a node describing behavior the code
no longer has, claims that were never accurate.

It also catches the failure `validate` structurally cannot: a node whose anchors all resolve
perfectly but whose description describes the wrong thing.

## Getting the nodes

Use `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" export -o .kg-export.json` for the whole graph as JSON, or `kg_find_by_kind` /
`kg_node` to pull the slice under audit. `kg_find_by_kind` and `kg_validate`'s issue lists return
50 entries by default, so when a result says `truncated`, pass a larger `limit` or page
`kg_find_by_kind` with `offset`. Export is usually right here — the audit reads every node
anyway, and the JSON is easy to partition across sub-agents. Delete the export when the sweep is
done; it is a snapshot, and building it later would revert whatever `refresh` did in the meantime.

The export is **read-only input to this skill**. An audit reports; it never writes, so it uses
neither the round trip's build step nor the MCP write tools. Findings go to `refresh`, which
picks the write path that fits the size of the fix.

## Workflow

### 1. Inventory
Run `kg_stats` for node count and sections. `kg_stats` and `kg_validate` run through the CLI, not
MCP, and print JSON. Arguments go in one JSON object after the tool name:

```bash
uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" query kg_stats
uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" query kg_validate '{"limit": 200}'
```

Read the `staleness` block of `kg_stats`: `stale_files` and
`stale_nodes` are the repo-wide count of mapped files whose contents no longer match what the graph
was built against, and `nodes` names the first of them. Those nodes are known-suspect before anyone
reads a line — verify them first and say so in the report.

**Size the sweep first.** With about 25 nodes or fewer, audit them yourself in this session: a
subagent per group costs more than the check itself. Above that, export the graph and partition the
nodes into ~4 balanced groups (by section, so each group is coherent).

### 2. Verify each group (parallelize above ~25 nodes)
Fan out one `Agent` sub-agent per group (read-only). On a graph of about 25 nodes or fewer, do the
same checks yourself, node by node. Each check, for every node in its group:

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
`refresh` to fix.

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

Verdict: <accurate | N findings — run refresh to reconcile>
```

## Rules

- **Source is ground truth.** When the graph and the code disagree, the code wins and the graph is
  the finding.
- **Read-only.** Do not edit the graph here. The audit's value is an honest, complete findings list.
- **No silent passes.** If a node couldn't be verified (source not reachable), say so — don't mark
  it accurate.
- **Don't audit for history.** A description saying nothing about *why* the code is the way it is
  correct behavior, not a gap. Ticket refs, dates and change narrative are excluded by design
  (`${CLAUDE_PLUGIN_ROOT}/SCHEMA.md` §5); flagging their absence is a misreading of the schema.

## Resources

- **`references/audit-pattern.md`** — the 4-agent partition recipe, sub-agent contract, and per-node
  verification checklist.

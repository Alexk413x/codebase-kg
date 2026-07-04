---
name: kg-validate
description: This skill should be used when the user asks to "validate the knowledge graph", "check the KG for drift", "lint the KNOWLEDGE_GRAPH.md", "find dangling edges or broken anchors", or "check the parity links" for a repo that already has a KG. It runs the fast, deterministic, STRUCTURAL codebase-kg drift checks (dangling edges, ungreppable symbol anchors, broken or non-reciprocal counterpart links, parity field mistakes) and reports — advisory only, never blocking. The cheap pre-check before the deeper kg-audit. (For the deep SEMANTIC accuracy sweep against source, use kg-audit instead.)
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - Read
  - Grep
  - Bash(git diff:*)
---

# kg-validate — deterministic drift check (advisory)

Run the codebase-kg validator over a repo's `KNOWLEDGE_GRAPH.md` and report. This is the **cheap,
deterministic** drift pre-check; `kg-audit` is the deeper source-vs-claim sweep. **Advisory only —
it never blocks a commit, build, or tool.**

## What it checks (via the MCP `kg_validate` tool)

- **Dangling edges** — an `edges` entry whose id has no node.
- **Counterpart problems** — a `counterpart` whose target file/`id` is missing, or that the peer KG
  doesn't link back to (a reciprocity break — see SCHEMA.md §8).
- **Parity field inconsistencies** — `divergent` without a `counterpart`/`divergence`; a
  `<codebase>-only` node that wrongly carries a `counterpart`; `matched` without a `counterpart`.
- **Ungreppable anchors** — a `path#Symbol` whose symbol no longer appears in the source file. The
  strongest signal a node has gone stale.

## Workflow

1. **Locate the KG.** If the user named a file, use it. Otherwise the MCP server auto-discovers the
   repo's `knowledge/KNOWLEDGE_GRAPH.md` (the only location — no repo-root fallback). Confirm with
   `kg_stats` — note the `refreshed` date and node count.
2. **Run `kg_validate`.** Call the MCP tool. It returns `dangling_edges`, `counterpart_issues`,
   `field_issues`, `anchor_issues`, plus `source_checked` (whether the source tree was reachable for
   the anchor check) and `anchors_checked`.
3. **If the MCP server is not available** (not installed, or KG path unresolved), fall back to a
   manual pass: `Read` the KG, build the node-id set, and `Grep` each `path#Symbol` anchor's symbol
   in its file. Report the same categories.
4. **Report** in the format below. Sort by severity: ungreppable anchors and dangling edges first
   (they break navigation), then counterpart/parity issues.
5. **Recommend, do not act.** Point each finding at the fix (usually `kg-refresh` for stale anchors,
   `kg-link` for counterpart issues). Do not edit the KG from this skill unless the user asks.

## Report format

```
## KG validation — <codebase> (refreshed <date>, <N> nodes)
Source check: <ran against source | skipped — source root not found>

Dangling edges (<n>):
- <node-id> → <missing-edge-id>

Ungreppable anchors (<n>):
- <node-id>: `<path#Symbol>` — symbol not found in file  → likely stale, run kg-refresh

Counterpart issues (<n>):
- <node-id>: <not reciprocal | target id not in peer KG | file not found>  → run kg-link

Parity field issues (<n>):
- <node-id>: <issue>

Verdict: <clean | N advisory findings — none blocking>
```

If `kg_validate` returns `ok: true`, say so plainly: "KG is structurally clean as of `<refreshed>`."

## Notes

- A skipped source check (`source_checked: false`) is not a failure — it means the anchor symbols
  couldn't be resolved to files (wrong `root`, or source not checked out). Say so; don't imply the
  anchors are fine.
- This skill reads; it does not write. Drift is surfaced as advice.

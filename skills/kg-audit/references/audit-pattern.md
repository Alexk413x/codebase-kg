# kg-audit — the multi-agent verification pattern

Loaded by `kg-audit`. The proven 4-agent source-vs-KG sweep.

## Why partition

A single agent auditing a large KG skims. Partitioning into ~4 coherent groups lets each agent read
its nodes' source end-to-end and verify claims deeply. Four is a starting point — scale groups so no
agent holds more than ~25 nodes.

## Partition

- Group by `###` section so each group is one coherent subsystem cluster (e.g. {entry, navigation},
  {domain, view-models}, {views, theme}, {services, data, DI}).
- Balance node counts across groups.

## Sub-agent contract (one per group, read-only)

Give each `Task` agent:

- The list of nodes in its group (id, kind, anchors, summary, edges).
- The repo `root`.
- This instruction set:

> For each node:
> 1. Grep every `anchors` symbol (`path#Symbol`) in its file. If the symbol is absent → **STALE**
>    (record the anchor).
> 2. Read the anchored source. Compare it to the node's `summary`, `kind`, and `edges`. Any claim
>    that the source contradicts → **INACCURATE** (record the exact wrong claim and what the source
>    actually shows — a version, a symbol name, a wiring path, a count).
> 3. Scan the node's directory for sibling source units of the same kind that have **no node** in the
>    provided list → candidate **MISSING** (record `path#Symbol` + kind).
> Return a findings table only. Do not edit anything. Source is ground truth.

Run all group agents concurrently (one message, multiple `Task` calls).

## Per-node verification checklist

- [ ] every anchor symbol greps in its file (else STALE)
- [ ] version/migration numbers in the summary match source (else INACCURATE)
- [ ] class/function/type names in the summary still exist (else INACCURATE)
- [ ] described wiring (who calls it, what it's injected into) matches source (else INACCURATE)
- [ ] edges reflect real current dependencies (else INACCURATE)
- [ ] no sibling unit of the same kind is undocumented (else MISSING)

## Consolidation

- Merge the group findings; dedup MISSING candidates against the full node set (an agent only saw
  its group, so it may flag a unit another group documents).
- Fold in `kg_validate`'s structural findings (dangling edges, non-reciprocal counterparts).
- Sort STALE → INACCURATE → MISSING → structural. Report; recommend `kg-refresh`.

## Honesty rules

- If an agent couldn't reach source for a node, that node is **unverified**, not accurate.
- Prefer false positives to false negatives: a flagged claim that turns out fine costs a glance; a
  missed stale claim is exactly the drift this skill exists to catch.

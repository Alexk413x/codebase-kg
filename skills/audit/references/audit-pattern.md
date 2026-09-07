# audit — the multi-agent verification pattern

Loaded by `audit`. The proven 4-agent source-vs-graph sweep.

## Why partition

A single agent auditing a large graph skims. Partitioning into ~4 coherent groups lets each agent
read its nodes' source end-to-end and verify claims deeply. Four is a starting point — scale groups
so no agent holds more than ~25 nodes.

## Partition

- Export the graph (`python -m codebase_kg.export -o .kg-export.json`) and group by `section`, so each
  group is one coherent subsystem cluster (e.g. {entry, navigation}, {domain, view-models},
  {views, theme}, {services, data, DI}).
- Balance node counts across groups.
- `rm .kg-export.json` when the sweep finishes. It is read-only input here — an audit never builds
  it back, and a stale copy left behind is a graph snapshot that still parses.

## Sub-agent contract (one per group, read-only)

Give each `Task` agent:

- The nodes in its group, as JSON (id, kind, description, anchors, edges).
- The repo `root`.
- This instruction set:

> For each node:
> 1. Grep every `anchors` symbol (`path#Symbol`) in its file. If the symbol is absent → **STALE**
>    (record the anchor).
> 2. Read the anchored source. Compare it to the node's `description`, `kind`, and `edges`. Any
>    claim the source contradicts → **INACCURATE** (record the exact wrong claim and what the source
>    actually shows — a symbol name, a wiring path, a behavior).
> 3. Scan the node's directory for sibling source units of the same kind that have **no node** in
>    the provided list → candidate **MISSING** (record `path#Symbol` + kind).
>
> Return a findings table only. Do not edit anything. Source is ground truth.
>
> Do **not** flag a description for omitting ticket ids, dates, or an account of what changed. Those
> are excluded by the schema on purpose; their absence is correct.

Run all group agents concurrently (one message, multiple `Task` calls).

## Per-node verification checklist

- [ ] every anchor symbol greps in its file (else STALE)
- [ ] class/function/type names in the description still exist (else INACCURATE)
- [ ] described behavior matches what the source does (else INACCURATE)
- [ ] described wiring (who calls it, what it's injected into) matches source (else INACCURATE)
- [ ] edges reflect real current dependencies (else INACCURATE)
- [ ] no sibling unit of the same kind is undocumented (else MISSING)

## Consolidation

- Merge the group findings; dedup MISSING candidates against the full node set (an agent only saw
  its group, so it may flag a unit another group documents). `kg_validate`'s `coverage.gaps` is
  the exact version of this list — prefer it, and use the agents' MISSING for judgement about which
  uncovered files actually deserve a node. When `coverage.declared` is `false`, that list is only as
  wide as the extensions already anchored, so the agents' MISSING is the *only* signal for a file
  type the graph has never touched.
- Fold in `kg_validate`'s other findings (ungreppable anchors, non-reciprocal counterparts).
- Sort STALE → INACCURATE → MISSING → structural. Report; recommend `refresh`.

## Honesty rules

- If an agent couldn't reach source for a node, that node is **unverified**, not accurate.
- Prefer false positives to false negatives: a flagged claim that turns out fine costs a glance; a
  missed stale claim is exactly the drift this skill exists to catch.

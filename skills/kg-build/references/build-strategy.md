# kg-build — partitioning & node-derivation detail

Loaded by `kg-build` when the repo is large enough that a single linear pass would be shallow.

## When to parallelize

- **< ~40 source files:** one linear pass. Read everything, emit nodes, assemble.
- **≥ ~40 files, or multiple distinct subsystems:** fan out. One sub-agent per subsystem keeps each
  agent's context focused and lets them read their files end-to-end instead of skimming.

## Partitioning

1. Inventory with `git ls-files` under `root`.
2. Bucket files into subsystems by directory + role. Typical buckets (rename to fit the repo):
   entry point, navigation/shell, domain model, view-models, views/components, services, data layer
   (db/network/storage), DI/modules, theme/styling, background work, widgets, utilities.
3. Each bucket becomes one `Task` sub-agent **and** one `###` section in the output.

## Sub-agent contract

Spawn read-only Explore-style agents. Give each:

- The subsystem's file list and `root`.
- The schema node shape (paste SCHEMA.md §4) and the hard rules: symbol anchors only (grep to
  confirm), no copied code, no line numbers, summary describes *this* codebase.
- Instruction to return nodes as schema-shaped key/value tables, plus the intra-subsystem edges.

Run buckets concurrently (one message, multiple `Task` calls). Collect all returned node blocks.

## Assembly

- Concatenate returned nodes under their `###` sections in a sensible reading order (entry →
  navigation → domain → view-models → views → services → data → DI → theme).
- Resolve **cross-subsystem edges**: a view-model node's `edges` will reference service/repo ids
  owned by another bucket. Verify each edge target id exists; fix mismatches (agents may have named
  the same concept differently — canonicalize the id).
- Write the `## EDGES` flows by tracing the main paths end-to-end across subsystems.

## Node-derivation checklist (per unit)

- [ ] `id` is a concept slug, stable across a file rename.
- [ ] `kind` is accurate free text.
- [ ] every `anchors` symbol was grep-confirmed in its file.
- [ ] `summary` is pointer-dense, no pasted code, describes behavior + wiring + any
      migration/version it introduced.
- [ ] `edges` list real intra-KG dependencies, all resolving to node ids.
- [ ] no line numbers anywhere.

## After assembly

Always finish with `kg_validate`. Dangling edges usually mean an id mismatch between buckets;
ungreppable anchors mean a symbol was guessed — re-grep and fix. Then `kg_stats` for the summary.

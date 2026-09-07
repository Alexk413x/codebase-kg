# build — partitioning & node-derivation detail

Loaded by `build` when the repo is large enough that a single linear pass would be shallow.

## When to parallelize

- **< ~40 source files:** one linear pass. Read everything, emit nodes, build.
- **≥ ~40 files, or multiple distinct subsystems:** fan out. One sub-agent per subsystem keeps each
  agent's context focused and lets them read their files end-to-end instead of skimming.

## Partitioning

1. Inventory with `git ls-files` under `root`.
2. Bucket files into subsystems by directory + role. Typical buckets (rename to fit the repo):
   entry point, navigation/shell, domain model, view-models, views/components, services, data layer
   (db/network/storage), DI/modules, theme/styling, background work, widgets, utilities.
3. Each bucket becomes one `Task` sub-agent **and** one `section` value in the output.

## Sub-agent contract

Spawn read-only Explore-style agents. Give each:

- The subsystem's file list and `root`.
- The JSON node shape and the hard rules: symbol anchors only (grep to confirm), no copied code, no
  line numbers, description is one line about *this* codebase, ≤ 240 chars, no ticket ids / dates /
  change narrative.
- Instruction to return **a JSON array of node objects** — nothing else, no prose wrapper — plus the
  intra-subsystem edges.

```json
[
  {
    "id": "feed_view_model",
    "kind": "ViewModel",
    "description": "Loads and ranks the main feed; exposes UI state and refresh.",
    "section": "VIEW MODELS",
    "anchors": ["ui/feed/FeedViewModel.kt#FeedViewModel"],
    "edges": ["feed_ranker", "acme_repository"]
  }
]
```

Run buckets concurrently (one message, multiple `Task` calls). Collect the returned arrays.

## Assembly

- Concatenate the returned arrays into one document's `nodes`, in a sensible reading order (entry →
  navigation → domain → view-models → views → services → data → DI → theme).
- Resolve **cross-subsystem edges**: a view-model node's `edges` will reference service/repo ids
  owned by another bucket. Agents may have named the same concept differently — canonicalize the id
  and fix every reference. The builder rejects the document if any edge target is unknown, so this
  cannot be skipped; finding them yourself just gives a better error than the builder's.
- Add the top-level config keys (`codebase`, `root`, `generated`, plus `language` / `counterpart` if
  they apply).

## Node-derivation checklist (per unit)

- [ ] `id` is a concept slug, stable across a file rename.
- [ ] `kind` is accurate free text.
- [ ] every `anchors` symbol was grep-confirmed in its file.
- [ ] `description` is one line, present tense, ≤ 240 chars, says what it is and what it does.
- [ ] `description` contains no ticket id, no date, no "now / previously / no longer".
- [ ] `edges` list real intra-graph dependencies, all resolving to node ids in the document.
- [ ] no line numbers anywhere.

## After assembly

Build with `python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db`, then
`rm .kg-export.json`. The builder is the first gate: it names any node that breaks a rule and writes
nothing. Then `kg_validate` — ungreppable anchors mean a symbol was guessed, so re-grep and fix.
Then `kg_stats` for the summary.

If `kg_validate` turns up a handful of fixable nodes, patch them with `kg_upsert_node` rather than
re-running the whole assembly: it is one atomic call per fix, it re-validates the graph before
landing, and it reports exactly what moved. Go back to the JSON only if the fix is structural.

---
description: Bootstrap a code graph for this repo from source — reads the tree and writes a committed, symbol-anchored knowledge/code_graph.db per the codebase-kg schema. Use on a repo with no graph yet.
argument-hint: "[path/subsystem to scope to | empty = whole repo]"
---

# /codebase-kg:build

Create a `knowledge/code_graph.db` for this repo from source.

**First: if `knowledge/KNOWLEDGE_GRAPH.md` exists, migrate instead of rebuilding** —
`python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md` converts it and preserves the curated
structure. Rebuilding from scratch throws that away.

1. Read `.claude/codebase-kg.local.md` if present (`codebase`, `root`, `graph_path`, `counterpart`,
   `language`); otherwise infer and confirm the config in one message.
2. Scope: if `$ARGUMENTS` names a path/subsystem, build just that; otherwise the whole `root`.
3. **Invoke the `kg-build` skill** and follow its workflow (survey → derive symbol-anchored nodes →
   author the JSON → `python -m codebase_kg.build` → `kg_validate`).
4. Add `knowledge/code_graph.db binary` to `.gitattributes` and commit the graph.

Posture: source-derived, symbol anchors only (never line numbers), point-don't-copy. Descriptions
are one line about what a thing is and does — no ticket ids, no dates, no change narrative (the
builder rejects all three). If a `counterpart` repo exists, build this side cleanly first, then use
`/codebase-kg:link`.

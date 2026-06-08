---
description: Bootstrap a knowledge graph for this repo from source — reads the tree and writes a symbol-anchored KNOWLEDGE_GRAPH.md per the codebase-kg schema. Use on a repo with no KG yet.
argument-hint: "[path/subsystem to scope to | empty = whole repo]"
---

# /codebase-kg:build

Create a `KNOWLEDGE_GRAPH.md` for this repo from source.

1. Read `.claude/codebase-kg.local.md` if present (`codebase`, `root`, `kg_path`, `counterpart`,
   `language`); otherwise infer and confirm the config in one message.
2. Scope: if `$ARGUMENTS` names a path/subsystem, build just that; otherwise the whole `root`.
3. **Invoke the `kg-build` skill** and follow its workflow (survey → derive symbol-anchored nodes →
   assemble per `templates/KNOWLEDGE_GRAPH.template.md` → `kg_validate`).

Posture: source-derived, symbol anchors only (never line numbers), point-don't-copy. If a
`counterpart` repo exists, build this side cleanly first, then use `/codebase-kg:link`.

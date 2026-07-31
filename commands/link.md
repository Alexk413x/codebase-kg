---
description: Establish or maintain cross-codebase parity between this repo's code graph and a paired repo's — reads both codebases and sets reciprocal counterpart + parity + divergence fields, then reports the gap list. Requires read access to both repos.
argument-hint: "[path to peer code_graph.db | empty = use meta counterpart]"
---

# /codebase-kg:link

Connect this graph to its peer so feature parity is queryable.

1. Resolve the peer graph: `$ARGUMENTS` if given, else this graph's `meta.counterpart` (or
   `.claude/codebase-kg.local.md`). Both codebases must be readable locally.
2. **Invoke the `kg-link` skill** and follow its workflow (pair config → match nodes by concept →
   classify matched vs divergent by reading source on both sides → write reciprocal counterpart /
   parity / divergence → build both graphs → `kg_validate` reciprocity → `kg_parity_gaps`).

The store accepts only three parity shapes and rejects the rest at build time: `matched` needs a
counterpart, `divergent` needs a counterpart *and* a divergence line, `<codebase>-only` must have
neither.

Posture: descriptive, not prescriptive. `<codebase>-only` flags are source-verified (grep the other
side to confirm true absence — a PRD mention doesn't count). Links are reciprocal. The gap report is
observation only — no work assigned.

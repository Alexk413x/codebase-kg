---
description: Establish or maintain cross-codebase parity between this repo's KG and a paired repo's KG — reads both codebases and sets reciprocal counterpart + parity + divergence fields, then reports the gap list. Requires read access to both repos.
argument-hint: "[path to peer KNOWLEDGE_GRAPH.md | empty = use header counterpart]"
---

# /codebase-kg:link

Connect this KG to its peer so feature parity is queryable.

1. Resolve the peer KG: `$ARGUMENTS` if given, else this KG's header `counterpart:` (or
   `codebase-kg.local.md`). Both codebases must be readable locally.
2. **Invoke the `kg-link` skill** and follow its workflow (pair headers → match nodes by concept →
   classify matched vs divergent by reading source on both sides → write reciprocal counterpart /
   parity / divergence → `kg_validate` reciprocity → `kg_parity_gaps`).

Posture: descriptive, not prescriptive. `<codebase>-only` flags are source-verified (grep the other
side to confirm true absence — a PRD mention doesn't count). Links are reciprocal. The gap report is
observation only — no work assigned.

---
description: Deep read-only accuracy sweep of this repo's KNOWLEDGE_GRAPH.md against current source — partitions the nodes across sub-agents and reports STALE / INACCURATE / MISSING. Advisory; makes no edits.
argument-hint: "[section/subsystem to scope to | empty = whole KG]"
---

# /codebase-kg:audit

Verify the KG still tells the truth about the code.

1. Scope: `$ARGUMENTS` (a section/subsystem) if given; otherwise the whole KG.
2. **Invoke the `kg-audit` skill** and follow its multi-agent verification sweep.

Posture: read-only, source is ground truth. Reports STALE (anchor symbol gone), INACCURATE (summary
contradicts source), MISSING (source unit with no node), plus `kg_validate`'s structural findings.
Recommend `/codebase-kg:refresh` to fix what it finds — this command does not edit.

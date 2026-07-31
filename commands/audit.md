---
description: Deep read-only accuracy sweep of this repo's code_graph.db against current source — partitions the nodes across sub-agents and reports STALE / INACCURATE / MISSING. Advisory; makes no edits.
argument-hint: "[section/subsystem to scope to | empty = whole graph]"
---

# /codebase-kg:audit

Verify the code graph still tells the truth about the code.

1. Scope: `$ARGUMENTS` (a section/subsystem) if given; otherwise the whole graph.
2. **Invoke the `kg-audit` skill** and follow its multi-agent verification sweep.

Posture: read-only, source is ground truth. Reports STALE (anchor symbol gone), INACCURATE
(description contradicts source), MISSING (source unit with no node), plus `kg_validate`'s findings.
Recommend `/codebase-kg:refresh` to fix what it finds — this command does not edit.

A description that says nothing about *why* the code changed is correct, not a gap: ticket ids,
dates and change narrative are excluded by the schema on purpose (`SCHEMA.md` §5).

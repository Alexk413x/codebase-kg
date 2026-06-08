---
description: Cheap deterministic drift check on this repo's KNOWLEDGE_GRAPH.md — dangling edges, ungreppable symbol anchors, broken or non-reciprocal counterpart links, and parity field mistakes. Advisory; reports only.
argument-hint: "[path to KNOWLEDGE_GRAPH.md | empty = auto-discover]"
---

# /codebase-kg:validate

Run the structural drift check.

1. Locate the KG: `$ARGUMENTS` if given, else the MCP server auto-discovers this repo's
   `KNOWLEDGE_GRAPH.md`.
2. **Invoke the `kg-validate` skill** — run the `kg_validate` MCP tool (or fall back to a manual
   Read + Grep pass) and report.

Posture: advisory, never blocks. For the deeper semantic accuracy sweep use `/codebase-kg:audit`;
to fix what's found use `/codebase-kg:refresh` (or `/codebase-kg:link` for counterpart issues).

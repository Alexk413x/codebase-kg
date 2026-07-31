---
description: Cheap deterministic drift check on this repo's code_graph.db — anchors that no longer resolve to source, source files no node covers, and broken or non-reciprocal counterpart links. Advisory; reports only.
argument-hint: "[path to code_graph.db | empty = auto-discover]"
---

# /codebase-kg:validate

Run the drift check against real source.

1. Locate the graph: `$ARGUMENTS` if given, else the MCP server auto-discovers this repo's
   `knowledge/code_graph.db`.
2. **Invoke the `kg-validate` skill** — run the `kg_validate` MCP tool and report.

What it looks for is only what the file cannot know about itself: do the anchors still point at real
code, is there source under `root` that no node covers, does the peer graph link back. Structural
integrity — unique ids, no dangling edges, consistent parity, symbol-only anchors — is guaranteed by
the store's constraints and reported as such rather than checked.

Posture: advisory, never blocks. For the deeper semantic accuracy sweep use `/codebase-kg:audit`; to
fix what's found use `/codebase-kg:refresh` (or `/codebase-kg:link` for counterpart issues).

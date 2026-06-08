---
description: Re-sync this repo's KNOWLEDGE_GRAPH.md with current source — comprehensively updates the affected nodes (add/edit/remove) and edges, then bumps the refreshed date. Never a header-only edit.
argument-hint: "[feature / commit-range to scope to | empty = since last refreshed]"
---

# /codebase-kg:refresh

Bring the KG back in sync with the code.

1. Scope the change set: `$ARGUMENTS` (a feature or commit range) if given; otherwise diff since the
   KG header's `refreshed:` date (`git diff --name-only <since>...HEAD`).
2. **Invoke the `kg-refresh` skill** and follow its workflow.

Hard rule (SCHEMA.md §6): update **all** affected nodes — add new, edit changed, remove deleted —
plus edges and flows. A header-or-date-only edit is forbidden. Finish with `kg_validate` and report
the node delta (+added / ~edited / -removed).

---
description: Re-sync this repo's code_graph.db with current source — comprehensively updates the affected nodes (add/edit/remove) and edges, then rebuilds the committed database.
argument-hint: "[feature / commit-range to scope to | empty = since the graph was generated]"
---

# /codebase-kg:refresh

Bring the code graph back in sync with the code.

1. Scope the change set: `$ARGUMENTS` (a feature or commit range) if given; otherwise diff since the
   graph's `generated` date from `kg_stats` (`git diff --name-only <since>...HEAD`). Map changed
   files to their nodes with `kg_find_by_path`.
2. **Invoke the `kg-refresh` skill** and follow its workflow.

Which of the two write paths to use — the write tools for one or two nodes, the export → edit →
build round trip for a whole change set — is the skill's call, and the skill states both in full.
It was restated here too, and in `skills/README.md`, and in `SCHEMA.md` §7.1; four copies of one
explanation is four places to miss when the round trip changes. Read it in
[`skills/kg-refresh/SKILL.md`](../skills/kg-refresh/SKILL.md).

Hard rule (`SCHEMA.md` §7): update **all** affected nodes — add new, edit changed, remove deleted —
plus edges. Finish with `kg_validate`, commit the `.db` alongside the source change, and report the
node delta (+added / ~edited / -removed).

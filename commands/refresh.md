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

The loop is export → edit → build:

```sh
python -m codebase_kg.export -o graph.json
#   … edit graph.json …
python -m codebase_kg.build graph.json -o knowledge/code_graph.db
```

Building an unedited export is byte-identical, so anything that appears in the git diff is a change
you actually made. The builder validates first and writes nothing if a node breaks a rule.

Hard rule (`SCHEMA.md` §7): update **all** affected nodes — add new, edit changed, remove deleted —
plus edges. Finish with `kg_validate`, commit the `.db` alongside the source change, and report the
node delta (+added / ~edited / -removed).

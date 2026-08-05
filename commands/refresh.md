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
python -m codebase_kg.export -o .kg-export.json    # scratch, gitignored
#   ... edit .kg-export.json ...
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                  # it is a snapshot, not a source
```

Delete it when you are done. A leftover export is a stale snapshot of the graph that still
parses, so a later `build` against it silently rebuilds from whenever it was written — a graph
that looks freshly built and is not. The dot-prefixed name keeps it out of casual `ls` and most
default ignore habits; add `.kg-export.json` to the repo's `.gitignore` as well.

Building an unedited export is byte-identical, so anything that appears in the git diff is a change
you actually made. The builder validates first and writes nothing if a node breaks a rule.

Hard rule (`SCHEMA.md` §7): update **all** affected nodes — add new, edit changed, remove deleted —
plus edges. Finish with `kg_validate`, commit the `.db` alongside the source change, and report the
node delta (+added / ~edited / -removed).

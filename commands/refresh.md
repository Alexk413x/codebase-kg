---
description: Re-sync this repo's code_graph.db with current source — comprehensively updates the affected nodes (add/edit/remove) and edges, then rebuilds the committed database.
argument-hint: "[feature / commit-range to scope to | empty = since the commit that last touched the graph]"
---

# /codebase-kg:refresh

Bring the code graph back in sync with the code.

1. Scope the change set: `$ARGUMENTS` (a feature or commit range) if given; otherwise diff from the
   commit that last touched the graph, which is the watermark for what it has already seen:

   ```sh
   SINCE=$(git log -1 --format=%H -- knowledge/code_graph.db)
   git diff --name-only "$SINCE"...HEAD
   ```

   Not the `generated` date — that needs a "commit near it", and `SCHEMA.md` §7 is explicit that the
   date is provenance rather than a freshness claim. An empty `$SINCE` means the graph has never been
   committed, and the scope is the whole repo. Map changed files to their nodes with
   `kg_find_by_path`.
2. **Invoke the `kg-refresh` skill** and follow its workflow.

**One or two nodes?** Use the write tools — `kg_upsert_node`, `kg_delete_node`, `kg_add_link`,
`kg_remove_link`. Each call is atomic, validates the whole graph before it lands, and reports every
field it changed, so the review the JSON diff gave you arrives in the tool's answer and there is no
scratch file to remember to delete.

**A whole change set?** That is what the round trip is for — a refresh usually touches several nodes
at once, and reading the JSON diff before building it is the point. The loop is export → edit →
build:

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

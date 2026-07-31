---
description: Make the committed code_graph.db readable in git diffs. Configures a textconv driver so git renders the graph as its JSON export in diff/show/log -p, instead of "Binary files differ". Local git config only — nothing is committed and nothing breaks for clones that skip it.
argument-hint: "[repo path | empty = current repo]"
---

# /codebase-kg:setup-diff

The graph is a SQLite file, so `git diff` says `Binary files a/knowledge/code_graph.db and
b/knowledge/code_graph.db differ` and a reviewer has to take the commit message on faith. A textconv
driver fixes that: git pipes each side through a converter and diffs the **text**, so a graph change
shows up as which nodes, anchors and edges moved.

Nothing about what is committed changes. `.db` stays binary in the repo; this only affects how git
*displays* it locally.

## Steps

### 1. Confirm `.gitattributes` marks the graph
The repo needs the `diff=codegraph` attribute. If `/codebase-kg:build` or `/codebase-kg:migrate`
set it up, it is already there:

```
*.db binary diff=codegraph
code_graph.db binary diff=codegraph
```

If it is missing, add it — this line is **committed**, so every clone gets the wiring. The driver
itself is per-clone config; a clone without it just sees the old binary behaviour.

### 2. Register the driver
`textconv` receives one path and must print the text form on stdout.

```sh
git config diff.codegraph.textconv "uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp codebase-kg-export"
git config diff.codegraph.binary true
```

`diff.codegraph.binary true` tells git the source really is binary, so it uses textconv for display
but never tries to generate a patch that could be applied back.

If the repo already has the package on its interpreter, prefer the faster form — `uvx` re-resolves
the environment on each invocation, which is noticeable across a long `git log -p`:

```sh
git config diff.codegraph.textconv "python -m codebase_kg.export"
```

### 3. Cache the conversion (optional, recommended)
```sh
git config diff.codegraph.cachetextconv true
```
Git then caches converted output per blob, so re-reviewing history does not re-export every time.

### 4. Verify
```sh
git diff HEAD~1 -- knowledge/code_graph.db
```
Expect JSON with `+`/`-` lines on the nodes that changed. If it still prints "Binary files differ",
check `git check-attr diff -- knowledge/code_graph.db` reports `diff: codegraph`.

## What this does not solve

Merge conflicts. Two branches that both refresh the graph still conflict with no textual merge, and
`git merge` cannot resolve it. The resolution is:

```sh
# During the conflict, git keeps both sides staged: :2 is ours, :3 is theirs.
git show :2:knowledge/code_graph.db > ours.db
git show :3:knowledge/code_graph.db > theirs.db
python -m codebase_kg.export ours.db   -o ours.json
python -m codebase_kg.export theirs.db -o theirs.json
# merge the JSON by hand, then rebuild and stage:
python -m codebase_kg.build merged.json -o knowledge/code_graph.db
git add knowledge/code_graph.db
```

That works because the build is deterministic — the same JSON always produces the same bytes — so a
rebuild after merging is reproducible rather than a third distinct artifact. See `docs/REVIEW.md`.

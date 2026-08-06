# Reviewing and merging a committed `code_graph.db`

The graph is a SQLite file that lives in git. That buys constant-time opens and
write-time integrity, and it costs the two things text gets for free: a readable
diff and a three-way merge. Both are recoverable, and the property that makes
them recoverable is determinism — **the same JSON always builds the same bytes**,
so a rebuild is reproducible rather than a third distinct artifact.

## Readable diffs

Out of the box:

```
$ git diff -- knowledge/code_graph.db
Binary files a/knowledge/code_graph.db and b/knowledge/code_graph.db differ
```

With the textconv driver configured (`/codebase-kg:setup-diff`):

```diff
$ git diff -- knowledge/code_graph.db
@@ -9,7 +9,7 @@
     {
       "id": "feed",
       "kind": "Domain",
-      "description": "Ranks the feed.",
+      "description": "Ranks the feed by recency and source weight.",
       "anchors": [
         "src/Feed.kt#Feed"
       ],
```

Git converts each side through `codebase-kg-export` and diffs the text. What is
committed does not change — `.db` stays binary in the repo, and a clone that
never configures the driver sees the old behaviour rather than an error.

Two pieces, deliberately split:

| Piece | Where | Committed? |
|---|---|---|
| `*.db binary diff=codegraph` | `.gitattributes` | **yes** — every clone gets the wiring |
| `diff.codegraph.textconv …` | `git config` | no — per-clone, because it names a local command |

The attribute cannot carry the command: the path to the converter differs per
machine, and a committed absolute path would be wrong for everyone else.

## Merges

Two branches that both refresh the graph conflict, and git cannot resolve it —
there is no textual merge for SQLite pages. Resolve through the JSON:

```sh
git show :2:knowledge/code_graph.db > ours.db      # :2 = ours
git show :3:knowledge/code_graph.db > theirs.db    # :3 = theirs
python -m codebase_kg.export ours.db   -o ours.json
python -m codebase_kg.export theirs.db -o theirs.json

# merge the two JSON documents, then:
python -m codebase_kg.build merged.json -o knowledge/code_graph.db
git add knowledge/code_graph.db
```

Because the build is deterministic, whoever resolves the conflict produces the
same bytes as anyone else resolving it the same way. That is what keeps this a
mechanical step rather than a judgement call about binary content.

### Why not commit the JSON instead?

It was considered and rejected. Committing `.kg-export.json` and generating the `.db`
via a `post-merge`/`post-checkout` hook gives real three-way merges — but hooks
do not run in CI, do not run for anyone who skips `/codebase-kg:install-hooks`,
and do not run on a plain `git archive` export. The MCP server needs the `.db`
on disk, so a missed rebuild is not a degraded experience, it is dead tooling
that fails at the moment someone is trying to get oriented.

Committing the artifact means the thing the tools read is always present and
always the thing that was reviewed. The costs are the two above, and both have a
documented answer.

## What a graph diff should look like

A refresh that changed nothing should produce **no diff at all**. If an unedited
`/codebase-kg:refresh` shows changes, something is wrong — the round trip is
byte-identical by design and there is a test pinning it
(`test_export_then_build_is_byte_identical`).

Expect to see, in review:

- `description` edits — the usual case
- `anchors` / `edges` changes — structure moved
- `sources` entries changing — the file behind a node was re-read and
  re-baselined. A `sources` change *without* a description change means someone
  ran `--rebaseline`, which asserts "I checked these" — worth a question if the
  diff shows nothing else.
- `generated` — always changes on a real refresh

### A diff from a write tool

`kg_upsert_node` and friends edit the graph in place instead of rebuilding it,
so the diff differs in two ways worth knowing before you read one.

- **`generated` does not move.** A targeted edit is not a build, and stamping a
  new date on one would claim the whole graph was re-derived. Provenance stays
  attached to the last actual build (`SCHEMA.md` §7 — nothing gates on the date).
- **The diff is smaller than a rebuild's.** Only the pages holding the changed
  rows move, which is exactly the property that keeps committed history small.
  A rebuild after a VACUUM rewrites the file; an edit does not.

Neither weakens review, because the tool reports every field it changed, before
and after, in its own answer — and it refuses the write outright if `kg_validate`
finds something the graph did not already have. A rejected call leaves the
committed file byte-identical, so there is no half-applied state to spot in a
diff.

## File size

Every refresh re-commits the whole file; SQLite does not delta-compress well.
On a graph of a few hundred nodes that is a few hundred KB per changed refresh —
noise at the scale of a source repo, but it is monotonic. If it ever matters,
`git gc --aggressive` packs the history, and the JSON export is always available
as a smaller archival form.

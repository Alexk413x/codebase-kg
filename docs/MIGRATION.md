# Migrating a repo from `KNOWLEDGE_GRAPH.md` to `code_graph.db`

One command per repo, then a commit. Nothing is destroyed along the way.

## Run it

```sh
cd <repo>
uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" migrate knowledge/KNOWLEDGE_GRAPH.md
```

To see the report without writing the file, add `--dry-run`.

It **converts rather than regenerates**. Ids, kinds, anchors, edges, sections and parity come across
verbatim — that structure is real work and there is no reason to re-derive it. Regeneration would
also confound three variables at once (a new store, a differently-worded graph, and genuine code
drift), making any difference impossible to attribute.

## What changes, and why

| | |
|---|---|
| `summary` → `description` | Scrubbed of ticket refs, dates and change narrative, then trimmed to ≤ 240 chars at a sentence boundary. That content duplicated git and was the entire maintenance burden (`SCHEMA.md` §5). |
| per-node `updated` dates | **Dropped.** Hand-maintained dates that nothing can verify. Staleness is now answered by checking anchors against source. |
| header refresh log | **Dropped.** Replaced by a single `generated` date. |
| dangling edges | **Dropped and listed.** The store's foreign keys make them unwritable. |
| inconsistent parity triples | **Normalized and listed.** The store accepts only the three legal shapes (`SCHEMA.md` §9). |
| line-number anchors | **Reduced to the path**, and listed. |
| `counterpart` paths | Retargeted from `…/KNOWLEDGE_GRAPH.md#id` to `…/code_graph.db#id`. |

Anything the scrubber cannot clean automatically is left empty and named under `needs_rewrite`, so
`/codebase-kg:refresh` can rewrite those few descriptions from source. It never invents text.

## After

1. **Check the report.** Node/edge/anchor counts should match what the markdown held. Dropped edges
   and normalized fields are listed individually — skim them; they are usually real defects the old
   format tolerated.
2. **Verify with the CLI.** `kg_stats` shows the shape, and `kg_validate` checks anchors against
   source:
   ```sh
   uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" query kg_stats
   uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" query kg_validate
   ```
3. **Mark it binary** in `.gitattributes`, routed to the textconv driver:
   ```gitattributes
   knowledge/code_graph.db binary diff=codegraph
   ```
4. **Commit the `.db`.**
5. **Delete `knowledge/KNOWLEDGE_GRAPH.md`** once you're satisfied. Migration leaves it untouched, so
   there is no hurry — but two artifacts is exactly the state this rewrite exists to avoid, and the
   MCP server only reads the `.db`.
6. **Run `/codebase-kg:setup`.** It wires the pre-commit and pre-push checks and the textconv
   driver. If the repo's `pre-push` has the old date-based freshness block, setup replaces that
   block with a call to the content-based check.

## If the repo has a wide-table graph

Some graphs use a wide, one-row-per-node table (`| id | kind | anchors | summary | edges | … |`)
rather than the vertical key/value form. The migration parser handles both.

Plugin versions before 0.2 read every row of a wide table as a node called `kind`, so every `kg_*`
tool returned nothing for that repo. If a repo's markdown graph never worked with the tools, this is
the likely cause, and migrating fixes it.

## Rollback

The markdown file is untouched, and the `.db` is a single file. `git rm` it, restore the previous
plugin version, and nothing else was modified.

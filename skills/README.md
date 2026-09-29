# skills/

Seven skills. Each is a `SKILL.md` (third-person trigger description + imperative workflow), with
the heavy multi-agent procedures pushed into `references/`. `setup` runs only when the user types
`/codebase-kg:setup` (`disable-model-invocation: true`), because it writes git hooks and git config.

| Skill | Use when | Writes? |
|---|---|---|
| [`build`](build/SKILL.md) | a repo has no graph (or too narrow a one) — bootstrap from source | writes `code_graph.db` |
| [`refresh`](refresh/SKILL.md) | code changed — re-sync the affected nodes | rebuilds `code_graph.db` |
| [`audit`](audit/SKILL.md) | deep read-only accuracy sweep — STALE / INACCURATE / MISSING | report only |
| [`link`](link/SKILL.md) | establish/maintain cross-codebase parity (counterpart links) | rebuilds both graphs |
| [`validate`](validate/SKILL.md) | cheap deterministic drift check against source | report only |
| [`query`](query/SKILL.md) | "where does X live", "what depends on X" — find code through the graph | report only |
| [`setup`](setup/SKILL.md) | wire the git hooks, the textconv driver and `install.sh` into a repo (user-invoked only) | writes `.githooks/`, `.gitattributes`, git config |

The six graph skills lean on the MCP query surface and the schema (`../SCHEMA.md`). List **both** server names
in `allowed-tools` — `mcp__codebase-kg__*` when the server is installed directly and
`mcp__plugin_codebase-kg_codebase-kg__*` when it arrives as a plugin. `test_plugin_surface.py`
asserts the two lists match, and that no registered tool is stranded outside every skill.

Each graph skill opens on an MCP call rather than a `Grep`, which also means the search gate has
granted credit before any search runs. Keep it that way when reordering steps — `build` is the one
exception and says so inline.

## Writing the graph

The artifact is SQLite, so no skill edits it with `Write`/`Edit`. There are two write paths, and the
size of the change picks between them.

**Targeted — the MCP write tools.** `kg_upsert_node`, `kg_delete_node`, `kg_add_link`,
`kg_remove_link`. Reach for these when a skill is fixing a handful of nodes: one description, an
anchor that moved, a link into another graph. Each call is atomic, runs `kg_validate` against the
result before it lands, and reports every field it changed.

**Bulk — the round trip.** Reach for this when the change is wholesale: a parity sweep, a
restructuring, a whole refresh's worth of nodes, or anything where reading the JSON diff before
building it is the point.

```sh
# The stdlib runner beside the MCP launcher works from any repo; it needs only uv.
uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" export -o .kg-export.json
uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                   # a snapshot, not a source
```

Both paths validate first and write nothing if a node breaks a rule, so a skill cannot ship a graph
with a dangling edge or a description carrying a ticket ref. The round trip is lossless, which means
an unchanged refresh produces an empty git diff; a rejected write tool call leaves the committed
file byte-identical.

## Division of labor

- **validate** — deterministic and cheap: do the anchors still resolve, is any source uncovered,
  does the peer link back. Structural integrity is guaranteed by the store, so it is reported rather
  than checked.
- **audit** — semantic: does the `description` still match what the source does? The only check
  that catches a node whose anchors resolve perfectly but whose claims are wrong.
- **refresh** — the only one that fixes drift in place.

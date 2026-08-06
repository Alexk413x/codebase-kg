# skills/

Five skills, all **advisory** and **source-derived**. Each is a `SKILL.md` (third-person trigger
description + imperative workflow), with the heavy multi-agent procedures pushed into `references/`.

| Skill | Use when | Writes? |
|---|---|---|
| [`kg-build`](kg-build/SKILL.md) | a repo has no graph (or too narrow a one) — bootstrap from source | writes `code_graph.db` |
| [`kg-refresh`](kg-refresh/SKILL.md) | code changed — re-sync the affected nodes | rebuilds `code_graph.db` |
| [`kg-audit`](kg-audit/SKILL.md) | deep read-only accuracy sweep — STALE / INACCURATE / MISSING | report only |
| [`kg-link`](kg-link/SKILL.md) | establish/maintain cross-codebase parity (counterpart links) | rebuilds both graphs |
| [`kg-validate`](kg-validate/SKILL.md) | cheap deterministic drift check against source | report only |

All five lean on the MCP query surface (`mcp__codebase-kg__*`) and the schema (`../SCHEMA.md`).

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
python -m codebase_kg.export -o .kg-export.json      # read  (kg-refresh, kg-link, kg-audit)
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                   # a snapshot, not a source
```

Both paths validate first and write nothing if a node breaks a rule, so a skill cannot ship a graph
with a dangling edge or a description carrying a ticket ref. The round trip is lossless, which means
an unchanged refresh produces an empty git diff; a rejected write tool call leaves the committed
file byte-identical.

## Division of labor

- **kg-validate** — deterministic and cheap: do the anchors still resolve, is any source uncovered,
  does the peer link back. Structural integrity is guaranteed by the store, so it is reported rather
  than checked.
- **kg-audit** — semantic: does the `description` still match what the source does? The only check
  that catches a node whose anchors resolve perfectly but whose claims are wrong.
- **kg-refresh** — the only one that fixes drift in place.

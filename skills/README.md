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

The artifact is SQLite, so no skill edits it with `Write`/`Edit`. The write path is always:

```sh
python -m codebase_kg.export -o graph.json      # read  (kg-refresh, kg-link, kg-audit)
python -m codebase_kg.build graph.json -o knowledge/code_graph.db
```

The builder validates first and writes nothing if a node breaks a rule, so a skill cannot ship a
graph with a dangling edge or a description carrying a ticket ref. The round trip is lossless, which
means an unchanged refresh produces an empty git diff.

## Division of labor

- **kg-validate** — deterministic and cheap: do the anchors still resolve, is any source uncovered,
  does the peer link back. Structural integrity is guaranteed by the store, so it is reported rather
  than checked.
- **kg-audit** — semantic: does the `description` still match what the source does? The only check
  that catches a node whose anchors resolve perfectly but whose claims are wrong.
- **kg-refresh** — the only one that fixes drift in place.

# MCP query surface — Phase 2 design

The MCP server is the "save on finding things" payoff: it parses one repo's `KNOWLEDGE_GRAPH.md`
(markdown is the source of truth) into an in-memory graph and exposes **read-only, typed
queries**, so an agent answers "where does X live / what depends on it / what diverges from the
peer" in one tool call instead of re-greps.

This is a **design spec**, not built yet. It mirrors the shape of the author's `a11y-kg` server.

## Packaging (mirror `a11y-kg`)

- Python package under `mcp/`, run via `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp codebase-kg <kg-path>`.
- `FastMCP("codebase-kg")` with one `@mcp.tool()` per query below; an in-memory graph loaded once
  at start from the markdown KG (a `loader.py` that parses the node tables + header per `SCHEMA.md`).
- `pyproject.toml`: `fastmcp`, `pydantic`, plus a markdown table parser (hand-rolled or a small dep).
  Console script `codebase-kg = codebase_kg.server:main`. Strict pyright, pytest.
- The KG path comes from (in order) a CLI arg, then `CLAUDE_PLUGIN_ROOT`/config, then a walk.
  Unlike `a11y-kg` (which serves one fixed graph shipped in the plugin), this server points at the
  **target repo's** KG — the path is per-project config (`SCHEMA.md` §7).

### Planned `.mcp.json` (added in Phase 2)

```json
{
  "mcpServers": {
    "codebase-kg": {
      "command": "uvx",
      "args": ["--from", "${CLAUDE_PLUGIN_ROOT}/mcp", "codebase-kg", "${KG_PATH}"]
    }
  }
}
```

`${KG_PATH}` resolves from `codebase-kg.local.md` (`kg_path`). Not committed until the package
exists, so plugin load doesn't fail on a missing server.

## Tools (build plan §5)

| Tool | Returns |
|---|---|
| `kg_search(query, kind?)` | Nodes by fuzzy match on `id` / `kind` / `summary`. Ranked, capped. |
| `kg_node(id)` | One full node — anchors, summary, edges, parity, counterpart, divergence. |
| `kg_neighborhood(id, depth=1)` | A node + its `edges` (and `counterpart`), N hops out. |
| `kg_find_by_kind(kind)` | All nodes of a given `kind` (free-text match). |
| `kg_parity_gaps(status?)` | All nodes flagged `divergent` / `*-only` across linked KGs — the gap report as a query, not a file. `status` filters to one shape. |
| `kg_stats()` | Counts: nodes, edges, parity breakdown, `refreshed` date. Cold-start health. |
| `kg_validate()` | Drift detector: dangling `edges`, dangling/one-directional `counterpart` links, and anchors whose `Symbol` no longer greps in source. |

## Cross-KG resolution

The server points at one KG by config but can **resolve `counterpart` links** into the paired KG
(load the peer file referenced in the header / on a node) — so `kg_parity_gaps` and
`kg_neighborhood` can answer over one codebase *or* the union of the pair. Reciprocity is the
convention; `kg_validate` reports asymmetric links.

## Validation = the drift detector

`kg_validate` is where "advisory, never blocking" pays off. It does not gate anything; it reports:

1. **Dangling edges** — an `edges` entry whose `id` has no node.
2. **Dangling counterparts** — a `counterpart` whose target file/`id` doesn't exist, or that the
   peer doesn't link back to (reciprocity break).
3. **Ungreppable anchors** — a `path#Symbol` whose `Symbol` no longer appears in `path` (the file
   moved/renamed or the symbol was deleted) — the strongest signal a node has gone stale.

The `kg-audit` skill (Phase 3) does the deeper, agent-driven source-vs-claim sweep; `kg_validate`
is the cheap deterministic pre-check.

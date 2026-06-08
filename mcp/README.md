# mcp/ — Phase 2 (not built yet)

The markdown-KG query server. Parses one repo's `KNOWLEDGE_GRAPH.md` into an in-memory graph and
exposes read-only typed queries. Mirrors the author's `a11y-kg` server (FastMCP, `uvx`-run,
strict pyright, pytest).

**Full design + tool list:** [`../docs/MCP_SURFACE.md`](../docs/MCP_SURFACE.md).

Planned layout when built:

```
mcp/
├── pyproject.toml            # fastmcp + pydantic + a markdown table parser; script: codebase-kg
├── src/codebase_kg/
│   ├── server.py             # FastMCP("codebase-kg"); one @mcp.tool() per query
│   ├── loader.py             # parse KNOWLEDGE_GRAPH.md (header + node tables) → Graph
│   ├── models.py             # Node / Edge / Graph (pydantic)
│   └── tools.py              # kg_search / kg_node / kg_neighborhood / kg_find_by_kind /
│                             #   kg_parity_gaps / kg_stats / kg_validate
└── tests/
```

A root `.mcp.json` registering the server is added **in this phase** (not before — a `.mcp.json`
pointing at a non-existent package would fail plugin load). See `MCP_SURFACE.md` for its shape.

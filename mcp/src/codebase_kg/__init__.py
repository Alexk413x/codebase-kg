"""codebase-kg — a local MCP server over a per-repo committed `code_graph.db`.

A single SQLite file is the source of truth (see ../../SCHEMA.md):

- `schema`   — the DDL, whose constraints make dangling edges, duplicate ids and
               inconsistent parity *unwritable* rather than merely reportable
- `models`   — `Node` / `Anchor` / `Meta`, the write shape
- `clean`    — the `description` contract and its scrubber
- `writer`   — transactional, deterministic build
- `store`    — read-only query facade (`CodeGraph`)
- `tools`    — the read-only queries the MCP tools wrap
- `server`   — FastMCP entry point
- `daemon`   — `--serve`: one server process shared by every session
- `shim`     — what `.mcp.json` launches: relays a session to the shared server
- `markdown` / `migrate` — one-time conversion from the pre-0.2 `KNOWLEDGE_GRAPH.md`

Everything except `server` and `daemon` depends only on the standard library,
so the graph logic is testable without FastMCP installed.
"""

__version__ = "0.2.0"

"""codebase-kg — a local MCP server over a per-repo committed `code_graph.db`.

A single SQLite file is the source of truth (see ../../SCHEMA.md):

- `schema`   — the DDL, whose constraints make dangling edges, duplicate ids and
               inconsistent parity *unwritable* rather than merely reportable
- `models`   — `Node` / `Anchor` / `Meta`, the write shape
- `clean`    — the `description` contract and its scrubber
- `writer`   — transactional, deterministic build
- `store`    — read-only query facade (`CodeGraph`)
- `tools`    — the read-only queries the MCP tools wrap
- `edits`    — the targeted writes
- `daemon`   — the server: `--serve` runs one process shared by every session
- `core`     — the tool catalog, argument checks and one tool call, for every transport
- `http_transport` / `tcp_transport` — MCP over HTTP for Claude Code, classic MCP for the shims
- `pool` / `worker` — the worker processes tool calls run on
- `resolve`  — which graph a session's calls use
- `shim`     — what stdio clients launch: relays a session to the shared server
- `server`   — the fastmcp tool definitions `catalog.json` is generated from
- `markdown` / `migrate` — one-time conversion from the pre-0.2 `KNOWLEDGE_GRAPH.md`

Everything except `server` depends only on the standard library. Nothing at
runtime imports `server`; it needs the dev dependencies.
"""

__version__ = "0.2.0"

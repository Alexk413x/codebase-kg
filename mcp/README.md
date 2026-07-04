# codebase-kg MCP server

A local **stdio** MCP server that parses one repo's `KNOWLEDGE_GRAPH.md` (markdown is the source
of truth — see [`../SCHEMA.md`](../SCHEMA.md)) into an in-memory graph and exposes **seven
read-only, typed queries**. So an agent answers "where does X live / what depends on it / what
diverges from the peer" in one tool call instead of re-grepping every session.

Mirrors the author's `a11y-kg` server (FastMCP, `uvx`-run). `loader` and `tools` are **stdlib
only** — testable without FastMCP installed; only `server` imports FastMCP.

## Tools

| Tool | Returns |
|---|---|
| `kg_search(query, kind?)` | Nodes by token match on id / kind / summary / anchors. Ranked, capped at 10. |
| `kg_node(id)` | One full node — anchors, summary, edges, parity, counterpart, divergence, inbound edges. `did_you_mean` on miss. |
| `kg_neighborhood(id, depth=1)` | A node + outbound/inbound edges + counterpart, N hops (1–2). |
| `kg_find_by_kind(kind)` | All nodes whose free-text `kind` matches (substring). |
| `kg_parity_gaps(status?)` | Nodes flagged `divergent` / `<codebase>-only` — the gap report as a query. |
| `kg_stats()` | Counts by kind / section / parity, edge total, `refreshed` date. |
| `kg_validate()` | Advisory drift check: dangling edges, counterpart problems (missing / not reciprocal), parity field inconsistencies, ungreppable anchors. Never blocks. |

## How it finds the KG

KG path resolution order:

1. First CLI arg (e.g. `codebase-kg /path/to/KNOWLEDGE_GRAPH.md`).
2. `$CODEBASE_KG_PATH`.
3. Walk up from the current working directory, honoring an optional `kg_path` override in
   `.claude/codebase-kg.local.md` (SCHEMA.md §7 — same file the hooks read), else
   `knowledge/KNOWLEDGE_GRAPH.md` (no repo-root fallback).

When run as a plugin MCP server (cwd = the user's project), step 3 finds the repo's KG with no
config. Set `CODEBASE_KG_PATH` to point at a KG elsewhere. The graph loads **lazily** and is
**re-read whenever the file changes** on disk (mtime/size) — a KG created after session start
(`/codebase-kg:build`) or edited mid-session is picked up on the next tool call, no restart. The
peer KG named in the header `counterpart:` is loaded and refreshed the same way for the
parity/reciprocity checks.

## Run

```bash
# from a repo that has a KNOWLEDGE_GRAPH.md (auto-discovered):
uvx --from /path/to/codebase-kg/mcp codebase-kg
# or point explicitly:
uvx --from /path/to/codebase-kg/mcp codebase-kg /path/to/KNOWLEDGE_GRAPH.md
```

The plugin wires this via the root [`../.mcp.json`](../.mcp.json) (`uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp …`).

## Develop / test

```bash
cd mcp
python -m pytest -q          # loader + tools (stdlib only — no FastMCP needed)
uv run --with fastmcp python -c "import codebase_kg.server"   # server import smoke
```

Layout:

```
mcp/
├── pyproject.toml            # fastmcp; console script: codebase-kg
├── src/codebase_kg/
│   ├── models.py             # Node / Header / Graph (dataclasses)
│   ├── loader.py             # parse KNOWLEDGE_GRAPH.md → Graph (new schema + legacy aliases)
│   ├── tools.py              # the 7 queries
│   └── server.py             # FastMCP("codebase-kg") wiring
└── tests/                    # fixtures = a cross-linked ios/android KG pair + a tiny source tree
```

The loader is tolerant of **legacy** hand-written field names (`type`/`files`/`details`/`deps`)
as well as the new schema (`kind`/`anchors`/`summary`/`edges`), so it parses existing hand-written
KGs during migration.

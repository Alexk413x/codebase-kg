# codebase-kg MCP server

A local **stdio** MCP server over one repo's committed `knowledge/code_graph.db` (see
[`../SCHEMA.md`](../SCHEMA.md)), exposing **eight read-only, typed queries** — so an agent answers
"where does X live / what depends on it / what diverges from the peer" in one tool call instead of
re-grepping every session.

Mirrors the author's `a11y-kg` server (FastMCP, `uvx`-run). Everything except `server` is **stdlib
only** — the whole graph layer is testable without FastMCP installed.

## Tools

| Tool | Returns |
|---|---|
| `kg_search(query, kind?)` | Ranked FTS5 search over id / kind / description / anchors. CamelCase identifiers match in split form ("video playback" finds `VideoPlaybackService`). Capped at 10. |
| `kg_node(id)` | One full node — anchors, description, edges, parity, counterpart, divergence, inbound edges. `did_you_mean` on miss. |
| `kg_find_by_path(path)` | **Reverse lookup**: which node(s) own a source file, and what connects to them. Accepts a repo-relative path or a bare filename. |
| `kg_neighborhood(id, depth=1)` | A node + everything within 1–3 hops, following edges either way, with hop counts. |
| `kg_find_by_kind(kind)` | All nodes whose free-text `kind` matches (substring). |
| `kg_parity_gaps(status?)` | Nodes flagged `divergent` / `<codebase>-only` — the gap report as a query. |
| `kg_stats()` | Counts by kind / section / parity, edge and anchor totals, isolated nodes, `generated` date. |
| `kg_validate()` | Advisory drift check against real source: ungreppable anchors, uncovered source files, counterpart problems. Never blocks. |

## CLIs

The server is read-only. Writing goes through three small commands:

```sh
python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md   # one-time: markdown → db
python -m codebase_kg.export -o .kg-export.json                   # db → JSON
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
```

`export` → `build` with no edits is byte-identical, so a no-op refresh leaves the git diff empty.

## How it finds the graph

Path resolution order:

1. First CLI arg (e.g. `codebase-kg /path/to/code_graph.db`).
2. `$CODEBASE_KG_PATH`.
3. Walk up from the current working directory, honoring an optional `graph_path` override in
   `.claude/codebase-kg.local.md` (SCHEMA.md §8 — same file the hooks read; the older `kg_path` key
   is still accepted), else `knowledge/code_graph.db`. **No repo-root fallback.**

While unresolved, the path is re-resolved on every tool call, so a graph created after the server
started (e.g. by `/codebase-kg:build`) is picked up without a restart. If the repo still has a
pre-0.2 `KNOWLEDGE_GRAPH.md`, the error names the migration command rather than just saying "not
found".

The graph is **opened per tool call and closed again**. That is affordable because opening a store
is constant-time (~1 ms) rather than a parse whose cost grows with the graph — there is nothing to
amortize. It also matters on Windows, where a held-open handle blocks the file from being replaced:
caching the connection would make `/codebase-kg:refresh` fail to write its own output whenever the
server was running.

## Module map

| Module | Role |
|---|---|
| `schema.py` | The DDL. Constraints make dangling edges, duplicate ids, inconsistent parity and line-number anchors unwritable. |
| `models.py` | `Node` / `Anchor` / `Meta` dataclasses — the write shape. |
| `clean.py` | The `description` contract (no ticket refs, dates or change narrative) and its scrubber. |
| `writer.py` | Transactional, deterministic build. Validates first; writes to a temp file and renames. |
| `store.py` | `CodeGraph` — read-only query facade over one connection. |
| `tools.py` | The read-only queries the MCP tools wrap. |
| `codec.py` | The JSON interchange shape shared by `build` and `export`. |
| `markdown.py` | The pre-0.2 markdown parser. **Migration only** — nothing on the query path imports it. |

## Development

```sh
uv sync
uv run pytest -q
```

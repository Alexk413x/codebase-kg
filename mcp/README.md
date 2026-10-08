# codebase-kg MCP server

A local MCP server over one repo's committed `knowledge/code_graph.db` (see
[`../SCHEMA.md`](../SCHEMA.md)), exposing **seven typed queries and six targeted writes** — so an
agent answers "where does X live / what depends on it / what diverges from the peer" in one tool
call instead of re-grepping every session, and fixes one wrong description without regenerating the
whole artifact.

The server and everything it imports are **stdlib only**: it runs on the base Python 3.10 or later,
with no venv. The tool definitions are fastmcp registrations in `server.py`, a dev-only module;
`scripts/gen_catalog.py` turns them into `src/codebase_kg/catalog.json`, which the server serves.

## Queries

| Tool | Returns |
|---|---|
| `kg_search(query, kind?)` | Ranked FTS5 search over id / kind / description / anchors. CamelCase identifiers match in split form ("video playback" finds `VideoPlaybackService`). Capped at 10. |
| `kg_node(id)` | One full node — anchors, description, edges, parity, counterpart, divergence, inbound edges. `did_you_mean` on miss. |
| `kg_find_by_path(path)` | **Reverse lookup**: which node(s) own a source file, and what connects to them. Accepts a repo-relative path or a bare filename. |
| `kg_neighborhood(id, depth=1)` | A node + everything within 1–3 hops, following edges either way, with hop counts. |
| `kg_find_by_kind(kind)` | All nodes whose free-text `kind` matches (substring). |
| `kg_find_by_link(target)` | **Reverse lookup across graphs**: which code node(s) point at a node in another committed graph in this repo. |
| `kg_find_by_reference(query?, kind?)` | **Reverse lookup by documentation**: which node(s) cite a URL or title matching `query`, with the `path` / `symbol` each citation narrows to. |

## CLI-only queries

These run through `launch/kg_cli.py query <tool> [json-args]` and are not in the MCP catalog. The
CLI prints the same JSON, from the same functions in `tools.py` (dispatched by `query.TOOLS`).

| Tool | Returns |
|---|---|
| `kg_parity_gaps(status?)` | Nodes flagged `divergent` / `<codebase>-only` — the gap report as a query. |
| `kg_stats()` | Counts by kind / section / parity, edge and anchor totals, isolated nodes, `generated` date, and `staleness` — the repo-wide count of mapped files that no longer match what the graph was built against, with the nodes that describe them. |
| `kg_validate()` | Advisory drift check against real source: ungreppable anchors, uncovered source files, counterpart problems. Never blocks. |

## Writes

For **targeted** changes — a handful of nodes, a link. Bulk work stays on the round trip below, and
each tool's description says so, because picking the wrong one is how this surface gets misused.

| Tool | Does |
|---|---|
| `kg_upsert_node(nodes)` | Creates or updates node(s). Only the keys supplied change; `null` clears a parity field; `anchors` / `edges` / `external_links` / `references` replace the whole list. A file the node anchors with no baseline gets one; `"rebaseline": true` re-hashes the node's files, which clears them for every node anchored there. |
| `kg_delete_node(ids, dry_run=True, cascade_inbound=False)` | Previews what cascades (anchors, outbound edges, external links, baselines of files no other node anchors) and what blocks (inbound edges, `ON DELETE RESTRICT`), then deletes. |
| `kg_add_link(node_id, target, kind='')` | Points a node at `<db-file>#<node-id>` in another committed graph. Refused if the peer graph is present and lacks that node. |
| `kg_remove_link(node_id, target)` | Drops one such pointer; the node is untouched. |
| `kg_add_reference(node_id, url, kind='', title='', path=None, symbol=None)` | Records where a fact the node depends on is documented. `path` / `symbol` must equal one of the node's anchors, or the write is refused. |
| `kg_remove_reference(node_id, url, path=None, symbol=None)` | Drops the node's reference(s) to `url`, or only the one with that narrowing. |

Three properties, all in `edits.py`:

- **Atomic.** The mutation runs against a private copy of the file inside one transaction, and the
  copy replaces the original only at the very end. A rejected edit leaves the committed graph
  byte-identical — not rolled back, never opened for writing.
- **Validated, not merely constrained.** `kg_validate` — the same function `kg_cli.py query kg_validate` calls — runs
  against the copy, and the write is refused if it introduced a finding the graph did not already
  have. The test is *no new findings*, never *clean*: a real graph carries findings, and demanding
  zero would lock the tools out of the graphs that need editing.
- **Reported.** Every call returns the rows it touched, field by field, before and after. That is
  the diff review the export path gave for free.

## CLIs

Whole-graph writes — migration, and the bulk round trip:

```sh
python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md   # one-time: markdown → db
python -m codebase_kg.export -o .kg-export.json                   # db → JSON
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                                # a snapshot, not a source
```

`export` → `build` with no edits is byte-identical, so a no-op refresh leaves the git diff empty.

## How it runs

One server process per machine and server build serves every session. A build is the
plugin version plus a short digest of the path, size and mtime of every `*.py` in the server package,
so a dev checkout and an installed copy at the same version never share a server.
Claude Code reaches it over HTTP, MCP 2026-07-28 (`.mcp.json`, `http_transport.py`). Stdio clients such as
Codex launch `mcp/launch/kg-shim`, which runs `shim.py` with the system Python:
`python3`, else `python`, on macOS and Linux, and `py -3`, else `python`, on Windows (`kg-shim.cmd`),
where a stock install has no `python3.exe` and both names may be Microsoft Store stubs. The shim is
stdlib only and does three things:

1. It reads `server-<build>.json` from the user cache directory (`%LOCALAPPDATA%\codebase-kg\` on
   Windows, `~/Library/Caches/codebase-kg/` on macOS, `$XDG_CACHE_HOME/codebase-kg/` or
   `~/.cache/codebase-kg/` elsewhere). The file names the port, pid and token of the running server.
2. If no live server answers, it takes a lock file and starts one, detached: the base interpreter
   of the Python running the shim, as `python -I -S -c <entry> <plugin>/mcp/src --serve`
   (`shim.server_command`). It starts in about 0.2 s. The server's log is `server-<build>.log` in
   the same directory.
3. It connects to `127.0.0.1:<port>`, sends a one-line handshake (the token, the build, the session's
   cwd and its explicit graph path), and then relays JSON-RPC unchanged in both directions.

The server listens on loopback only, on a port the OS picks, and refuses a handshake with the wrong
token or build. The state file is readable only by the user. Each connection is its own classic MCP
session (`initialize`, `tools/list`, `tools/call`), served by `tcp_transport.py` from the same
`core.Core` as HTTP. A server without the HTTP port exits after 10 minutes with no connections; one
with it, after 8 hours with no request and no connection. Either removes its state file.

A session never loses its tools. If the shared server cannot be reached within 10 seconds
(`CODEBASE_KG_SHARED_TIMEOUT`), the shim runs a private stdio server for that session instead. A
shim that started the server itself waits up to 25 seconds (`CODEBASE_KG_SPAWN_TIMEOUT`) first,
so a slow start does not make it start a second server.

A session also survives a crashed shared server. The shim records the session's `initialize` request
and `notifications/initialized`, and tracks which requests await a response. If the server hangs up
while the session is open, the shim:

1. Answers each request in flight with a JSON-RPC error that says to retry the call.
2. Reconnects the way it first connected, starting a new server if none answers.
3. Replays `initialize` and `notifications/initialized`, and drops the new server's `initialize`
   result, so the session sees only one.
4. Sends whatever the session wrote while it reconnected.

If no shared server comes back, the rest of the session runs on a private server with the same replay.
When the session closes its stdin, the shim exits and does not reconnect.

| Variable | Effect |
|---|---|
| `CODEBASE_KG_SHARED=0` | Skip the shared server; run a private stdio server for this session. |
| `CODEBASE_KG_SHARED_TIMEOUT` | Seconds the shim waits for the shared server before it falls back. Default 10. |
| `CODEBASE_KG_IDLE_TIMEOUT` | Seconds the shared server stays up with no connections. Default 600. |
| `CODEBASE_KG_HTTP_IDLE_TIMEOUT` | Seconds a server that holds the HTTP port stays up with no request and no connection. Default 28800 (8 hours). |
| `CODEBASE_KG_MAX_WORKERS` | Worker processes for tool calls, overriding the `max_workers` setting. Default 8; 0 runs calls in the server. |
| `CODEBASE_KG_CALL_TIMEOUT` | Seconds a worker may take for one call before it is killed. Default 60. |
| `CODEBASE_KG_PORT` | The HTTP port, overriding the `server_port` setting. 0 lets the OS pick. |
| `CODEBASE_KG_CACHE_DIR` | Where the state, lock, token and log files live. |

`kg_cli.py server status` prints the build, pid and `max_workers` of the server on the HTTP port;
`kg_cli.py server stop` stops it, so the next one reads the settings afresh. Run
`python -m codebase_kg.daemon`, with `src` on the path and no `--serve`, to get a private stdio server.

## The HTTP front

`http_transport.py` serves MCP 2026-07-28 as Claude Code 2.1.293 sends it: one self-contained POST
per request, no sessions. It answers `server/discover`, `tools/list`, `tools/call` and the empty
prompt and resource lists; a notification gets 202. It advertises only the tools capability.

| Request | Answer |
|---|---|
| Body is not JSON | 400, `-32700` |
| Body is not one JSON-RPC message | 400, `-32600` |
| `params._meta` lacks the protocol version or client capabilities | 400, `-32602` |
| A header contradicts the body (`mcp-protocol-version`, `mcp-method`, `mcp-name`) | 400, `-32020` |
| Any protocol version but 2026-07-28, including a classic `initialize` | 400, `-32022`, with the supported versions |
| Unknown method | 404, `-32601` |
| `GET /mcp` | 405 |
| An `Accept` without JSON | 406 |
| Body over 8 MB | 413 |
| Wrong `Host`, foreign `Origin`, missing or wrong token | 403 |
| Tool error or refused write | 200, a result with `isError: true` |

The first tool call from an unseen client returns `resultType: "input_required"` with a roots
request; the retry carries the roots in `inputResponses`. Request bodies may be chunked. A keep-alive
connection idle for 60 s is closed.

Arguments are checked against each tool's `inputSchema` in `core.py`, with pydantic's lax rules: an
integral float or a numeric string is an integer, `"true"`, `"off"`, `1` and `0` are booleans, an
unknown argument is refused, and a missing one takes its default. `tests/test_catalog.py` compares
the outcome with fastmcp's for each such case.

## How it finds the graph

Path resolution order:

1. First CLI arg (e.g. `codebase-kg /path/to/code_graph.db`).
2. `$CODEBASE_KG_PATH`.
3. Walk up from the current working directory, honoring an optional `graph_path` override in
   `.claude/codebase-kg.local.md` (SCHEMA.md §8 — same file the hooks read; the older `kg_path` key
   is still accepted), else `knowledge/code_graph.db`. **No repo-root fallback.**

The shared server applies the same order to each connection, using the session's CLI arg,
`$CODEBASE_KG_PATH` and cwd from the handshake, or an HTTP client's `X-Codebase-KG-Graph` header and
roots. It never reads its own cwd, environment or argv, so two sessions in two repos each see only
their own graph. `resolve.py` holds the order, for the server and for `server.py` alike.

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
| `server.py` | The fastmcp tool definitions `catalog.json` is generated from. Dev only: nothing at runtime imports it. |
| `catalog.json` | The `tools/list` result and the server instructions the server sends. |
| `daemon.py` | The server: `--serve` runs the shared one (state file, idle and orphan exit); without it, a private stdio server. |
| `core.py` | The catalog, argument checks, and one tool call on the pool, for every transport. Writes to one graph run one at a time. |
| `http_transport.py` | MCP over HTTP for Claude Code: Host, Origin and token checks, `/health`, `/shutdown`, roots binding, port claim and handover. |
| `tcp_transport.py` | Classic MCP over lines: the shims' TCP listener and handshake, and the private stdio server. |
| `pool.py` / `worker.py` | The elastic worker pool and the worker that runs read and write tools. |
| `resolve.py` | Which graph a session's calls use. |
| `control.py` | `kg_cli.py server status` and `server stop`. |
| `shim.py` | What stdio clients launch: finds or starts the shared server and relays the session to it. Stdlib only. It also holds the HTTP port, token and `/health` helpers that the hooks and `kg_headers.py` load by path. |
| `tools.py` | The read-only queries the MCP tools wrap. |
| `edits.py` | Targeted writes: copy, mutate in one transaction, validate, swap in — or discard, leaving the committed file byte-identical. |
| `codec.py` | The JSON interchange shape shared by `build` and `export`. |
| `markdown.py` | The pre-0.2 markdown parser. **Migration only** — nothing on the query path imports it. |

## Development

```sh
uv sync
uv run pytest -q
uv run python scripts/gen_catalog.py   # after changing a tool in server.py
```

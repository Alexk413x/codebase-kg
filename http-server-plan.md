# Shared HTTP server: plan

Status: Phases 0, 1 and 2 and the lean front done on `feat/push-refresh`, 2026-10-07; Phase 3 not
started.
Written 2026-10-07. Companion to `cli-plan.md`, whose benchmarks this plan builds on.

## Goal

Keep MCP speed for the lookups an agent makes while it works, with the least memory and context:

1. Sessions reach one shared server over HTTP. No process per session.
2. The server runs tool calls on an elastic pool of worker processes, so many agents at once don't
   queue behind one Python interpreter.
3. Tools that run once at the start or end of a task move to the CLI and leave the model's context.
4. The port is a plugin setting with a default that avoids common dev-tool ports.

## Why

Measured in `cli-plan.md`, 2026-10-07, Windows 11:

| Today (stdio shim per session) | Cost |
|---|---|
| One session | 116 MB across 4 processes |
| 8 sessions | 292 MB across 18 processes |
| Each extra session | One shim, 12-22 MB, whether or not it calls a tool |
| 16 agents calling at once | Median call 348-419 ms, slowest 2-2.5 s, because one interpreter serves every session |

The tool work itself is small: one plain-Python process making 30 calls peaks at 29 MB.

## Decisions

| Decision | Choice |
|---|---|
| Transport for Claude Code | Streamable HTTP on `127.0.0.1`, declared in `.mcp.json` as `"type": "http"` |
| Port | Plugin setting `server_port`, default **47821** |
| Other clients (Codex, sentinel-swarm role sessions, any stdio client) | Keep `kg-shim` and the TCP daemon path unchanged |
| Lookups (`kg_search`, `kg_node`, `kg_neighborhood`, `kg_find_by_kind`, `kg_find_by_path`, `kg_find_by_link`, `kg_find_by_reference`) | Stay on MCP |
| Start and end tools (`kg_stats`, `kg_validate`, `kg_parity_gaps`) and writes (`kg_upsert_node`, `kg_delete_node`, `kg_add_link`, `kg_remove_link`, `kg_add_reference`, `kg_remove_reference`) | Move to `kg_cli.py`, called from skills and hooks |
| Worker pool | Elastic: 0 workers when idle, up to `max_workers` (setting, default 8, no upper limit), a worker exits after 60 s idle |
| Server process | Standard library only, on the base Python with no venv (see Lean front) |
| A worker budget shared across plugins | Later option. If several plugins get pools, one machine-wide budget could cap their workers together; today each server caps only its own. |

### Ports

Defaults sit below the Windows dynamic range (49152-65535, where Hyper-V and WSL reserve blocks) and
away from common dev defaults (3000-3010, 4200, 5000, 5173, 6006, 6274, 6277, 8000-8100, 8080, 8888,
9000, 9229, 11434, 24678, 35729). Ports 47800-47899 were free on the development machine; they were
not checked against the IANA registry. One block serves every plugin in this workspace, so they
never collide with each other:

| Port | Server |
|---|---|
| 47821 | codebase-kg |
| 47822 | cartographer |
| 47823 | a11y tools |
| 47824 | a11y-kg |
| 47825 | android-driver-kg |
| 47826 | ios-driver-kg |
| 47827 | web-driver-kg |
| 47828 | ide-agent-tabs |
| 47829 | reserved |

Only codebase-kg changes in this plan. The others adopt their port when they move to HTTP.

## Verified facts this rests on

From the Claude Code docs (`mcp.md`, `plugins/manifest-reference.md`, `hooks.md`), checked 2026-10-07:

- A plugin's `.mcp.json` supports `"type": "http"` with a `url` and `headers`.
- `url` and `headers` expand `${VAR}`, `${VAR:-default}`, `${CLAUDE_PLUGIN_ROOT}` and
  `${user_config.KEY}`. They do **not** expand `${CLAUDE_PROJECT_DIR}` or `${CLAUDE_PLUGIN_DATA}`.
- `plugin.json` `userConfig` declares settings with a `type` (`string`, `number`, `boolean`,
  `directory`, `file`) and a `default`. Hooks read them as `CLAUDE_PLUGIN_OPTION_<KEY>`; skills as
  `${user_config.KEY}`.
- `headersHelper` runs a command on each connect and uses its JSON stdout as headers. It gets
  `CLAUDE_CODE_MCP_SERVER_URL`, but can't reference `${user_config.*}`.
- `SessionStart` command hooks run before MCP servers connect.
- An open bug report says Claude Code's HTTP client can drop the `Mcp-Session-Id` header.

## Phase 0: test what the docs leave open

Run each check with a throwaway HTTP MCP server that logs every request's headers, plus `claude -p`.
Record the answers in this file before Phase 1 starts.

| Question | Why it matters | Fallback if the answer is no |
|---|---|---|
| Does `headersHelper` run with the session's working directory, or see `CLAUDE_PROJECT_DIR`? | The server must know each session's repo to find its graph. Today the shim sends the cwd. | Ask the client for its roots (`roots/list`) when the session starts. If Claude Code doesn't answer that either, keep the shim for Claude Code. |
| Does `${user_config.server_port}` expand in a plugin `url`, with its default when the user never set it? | The port setting depends on it | `${CODEBASE_KG_PORT:-47821}` in the url, documented as the override |
| If the server isn't listening when the session connects, does Claude Code retry, or does the server stay failed? | Decides who must start the server | A `SessionStart` hook starts it, since that runs first |
| After the server restarts, does the next tool call reconnect, and does `headersHelper` run again? | Decides whether the server may exit when idle | The server doesn't exit while it has had a request in the last 8 hours |
| Does `Mcp-Session-Id` reach the server on every request? | Per-session state | Key per-session state on the helper's header instead |

## Phase 0 results

Measured 2026-10-07 on Windows 11 with Claude Code 2.1.293, `claude -p --model haiku`, a scratch
plugin loaded with `--plugin-dir`, and a throwaway fastmcp server that logged every request's
headers and body. Each run started `claude` with every `CLAUDE*`, `PWD` and `MCP_TIMEOUT` variable
removed from its environment, so nothing leaked in from the session that ran the tests.

**Claude Code speaks MCP protocol `2026-07-28` over HTTP.** It opens with `server/discover`, not
`initialize`. Every request is a self-contained POST carrying `mcp-protocol-version: 2026-07-28`,
`mcp-method` and, for a tool call, `mcp-name`. There is no `initialize`, no `Mcp-Session-Id`, and no
server-to-client request channel. The answers below follow from that.

| Question | Answer | Evidence |
|---|---|---|
| 1. Does `headersHelper` see the session's working directory or `CLAUDE_PROJECT_DIR`? | **No.** Its cwd is the plugin root and its environment has no `CLAUDE_PROJECT_DIR` and no session id. `${CLAUDE_PROJECT_DIR:-none}` and `${PWD:-none}` in `headers` expand to `none`. **The roots fallback works, in its 2026-07-28 form:** a server-to-client `roots/list` request fails (`NoBackChannelError`), but Claude Code answers a `ListRootsRequest` embedded in an `InputRequiredResult` to `tools/call`. It retries the call with `inputResponses` holding one root, the session's cwd as a `file:///` URI. The extra round trip took 10 ms. | Helper log: `cwd` = plugin folder, env = `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_MCP_SERVER_NAME`, `CLAUDE_CODE_MCP_SERVER_URL`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_PLUGIN_ROOT`, `MCP_TIMEOUT`, plus the inherited environment. Roots probe: first `tools/call` at 08.439, retry with `inputResponses.roots` = `file:///…/phase0/repo` at 08.449. |
| 2. Does `${user_config.server_port}` expand in a plugin `url`, with its default? | **Yes.** With the setting never set, `http://127.0.0.1:${user_config.server_port}/mcp` became `http://127.0.0.1:47890/mcp` (the scratch default). A user-set value was not tested. | Debug log: `Initializing HTTP transport to http://127.0.0.1:47890/mcp`. |
| 3. If the server isn't listening at connect, does Claude Code retry? | **Briefly, then it gives up for the session.** It tried at +0 s, +0.8 s, right after the `SessionStart` hooks finished (+2.5 s) and at +7.3 s, running `headersHelper` before each try, then logged `still failed after all retries`. A server that came up at +9 s was used; one that came up at +40 s never was (the model saw no tool). **`SessionStart` hooks do not run before MCP servers connect**: the first connect began about 0.7 s before the hook ran. Claude Code does reconnect once the hooks finish. | `late` run: helper at 148.5, 149.5, 151.3, 156.2; server up at 156.1; call succeeded. `late40` run: helper at 636.0, 636.8, 638.5, 643.4; `Retry: 1 remote server(s) still failed after all retries`; reply `NOTOOL`. |
| 4. After the server restarts, does the next call reconnect, and does `headersHelper` run again? | **A restarted server is reached with no reconnect at all**: the next `tools/call` went to the new process with no new `server/discover` and no `headersHelper` run. **A server that stays down is not restarted by anything**: each call fails with `ECONNREFUSED`; after the second, Claude Code reconnects with back-off (0, 1, 2, 4, 8 s, five tries, `headersHelper` each time) and then reports `is not connected`. | `restart` run: server stopped at 439.7, started at 442.7; call at 468.6 succeeded, helper log has one line. `kill` run: `Terminal connection error 1/3`, `2/3`, `Reconnect attempt 2/5` … `4/5`. |
| 5. Does `Mcp-Session-Id` reach the server on every request? | **No request carries one**, because protocol 2026-07-28 has no sessions. No header in Claude Code's requests tells two sessions apart, so per-session state must key on a header the helper adds. | Server log: no `mcp-session-id` header on any request. |

Other findings:

- `headersHelper` runs once per connect attempt, not per request, and its JSON output becomes
  headers on every request until the next reconnect.
- The hooks' launcher string (`command -v py … && py -3 … || python3 … || python …`) works as a
  `headersHelper` command, so it runs through a POSIX shell, as hooks do. `${CLAUDE_PLUGIN_ROOT}`
  expands in it.
- The helper inherits Claude Code's environment, so `CODEBASE_KG_PATH` reaches it.

### What changes in Phase 1

- **Step 4 (per-session graph).** The helper cannot send the cwd. It sends
  `X-Codebase-KG-Client`, a random id per helper run, and `X-Codebase-KG-Graph` when
  `CODEBASE_KG_PATH` is set. On the first tool call from a client id it has not seen, the server
  answers with an `InputRequiredResult` asking for roots, binds the first `file://` root as that
  client's cwd, and caches it. Every later call from that client binds from the cache with no extra
  round trip. A client that sends `X-Codebase-KG-Cwd` (an absolute path) skips the roots request,
  for HTTP clients other than Claude Code.
- **Idle exit.** A restarted server is reached without reconnecting, but a stopped one is not
  restarted mid-session. So the fallback applies: a server that holds the HTTP port exits only after
  8 hours with no HTTP request and no shim connection. A server without the port keeps today's rule.
- **Starting the server.** The `SessionStart` hook still starts it, as planned: Claude Code reconnects
  as soon as the hooks finish, inside its retry window. A cold first start that builds the venv can
  take longer than that window (about 7 s); that session then has no codebase-kg tools until the next
  one starts.

## Phase 1: HTTP transport

1. **Server.** `codebase-kg --serve` also serves Streamable HTTP at `http://127.0.0.1:<port>/mcp` with
   the same tool registrations. It keeps the TCP listener for shims. One process.
2. **Starting it.** A `SessionStart` hook (`hooks/kg_server_start.py`, stdlib only) checks
   `GET /health`. When nothing answers, it starts the server detached and waits up to 3 s. The hook
   exits 0 either way and prints one line only when it fails.
3. **Security.**
   - Bind to `127.0.0.1` only.
   - Refuse a request whose `Host` isn't `127.0.0.1:<port>` or `localhost:<port>`, or whose `Origin`
     is set to anything else. This blocks DNS-rebinding requests from a browser, which the MCP spec
     requires.
   - Require `Authorization: Bearer <token>`. The server writes the token to its per-user state
     file, readable only by the user, as it does today. `headersHelper` (`mcp/launch/kg_headers.py`)
     reads the token and prints the headers.
4. **Per-session graph.** `headersHelper` also sends `X-Codebase-KG-Cwd` and, when set,
   `X-Codebase-KG-Graph` (from `CODEBASE_KG_PATH`). The server binds them to the session the way the
   shim handshake does today (`server.Connection`). This depends on Phase 0, question 1.
5. **Port in use.** `/health` returns `{"service": "codebase-kg", "build": …}`. When the port answers
   as something else, the hook prints one line naming the `server_port` setting. When it answers
   as another codebase-kg build, a newer build asks the older one to stop (`POST /shutdown` with the
   token) and takes the port. An older build leaves a newer one running.
6. **Settings.** `plugin.json` gains `userConfig`:
   - `server_port`: number, default 47821
   - `max_workers`: number, default 4
7. **`.mcp.json`.** `"type": "http"`, `"url": "http://127.0.0.1:${user_config.server_port}/mcp"`, and
   `"headersHelper"` pointing at `kg_headers.py`.

## Phase 1 results

Built as steps 1-7 describe, with these changes:

| Change | Why |
|---|---|
| The helper sends `X-Codebase-KG-Client` (random per connect), not the cwd. The server asks for roots through an `InputRequiredResult` on a client's first tool call and caches the root per client id. `X-Codebase-KG-Cwd` is still honoured for other HTTP clients. | Phase 0, question 1 |
| The HTTP token is one per user (`<cache dir>/http-token`), not the per-process token in the state file. | Phase 0, question 4: Claude Code keeps the headers it got at connect and sends them to a restarted server, which would refuse a per-process token. |
| `POST /shutdown` takes the server's own state-file token, and the newer build sends it only to a holder whose pid and port match a live state file of this user. `/health` also returns `version`, `built` and `pid`. | A program squatting on the port must never receive a credential. |
| A build name taken from `/health` must match the build format before it names a state file, and the state file's own `version` must equal it. | A crafted build such as `../x` could otherwise point the check at any file. |
| A missing or wrong token gets 403, not 401. | A 401 starts Claude Code's OAuth flow, which this server does not offer. |
| A server of the same build that finds the port held by a verified server of its build exits at once. | It would otherwise overwrite the state file the shims and the helper read with one that holds no HTTP port. |
| `GET /health` treats a connect that takes over 0.25 s as nothing listening. | On Windows a refused loopback connect takes about 1 s, so the hook read a free port as "another program". |

Verified end to end on 2026-10-07: `claude -p --plugin-dir <this repo>` in sentinel-swarm, with the
installed plugin's environment variables removed. The hook started the server, Claude Code connected
on its fourth try (the first start built a fresh venv in about 8 s, so the hook printed its one
line), answered the roots request, and `kg_search` returned a sentinel-swarm node. A second session
connected on its first try in 292 ms, the helper took 266 ms, and the hook printed nothing.

### Benchmarks

`mcp/bench/cli_vs_mcp.py` and `mcp/bench/concurrency.py` gained an `http` mode: one server started
the way the hook starts it, with its own cache dir and an OS-picked port, and one fastmcp client per
session sending the helper's headers and the repo as its root. Cartographer's graph (1.14 MB),
Windows 11, 16 CPUs at about 19 % load from other work. `concurrency.py` ran twice; ranges cover
both runs.

| Measure | Stdio shim | HTTP | Target |
|---|---|---|---|
| Memory, 1 session | 120 MB, 4 processes | 90 MB, 2 processes | |
| Memory, 8 sessions | 317 MB, 18 processes | 94 MB, 2 processes | below 80 MB: **missed** |
| Connect | 2,391 ms (cold server) | 101 ms (warm server) | |
| Warm call, one session (`kg_search`, `kg_node`, `kg_neighborhood`) | 4.3-5.3 ms | 7.8-9.1 ms | |
| Call median, 1 agent | 9.0-10.7 ms | 14.4-17.2 ms | no slower than 22-25 ms: **met** |
| Call median, 4 agents | 31-42 ms | 48-51 ms | |
| Call median, 8 agents | 112-126 ms | 136-148 ms | |
| Call median, 16 agents | 222-228 ms | 306-325 ms | below 150 ms: **missed**, as expected before Phase 2 |
| Slowest call, 16 agents | 1,577-1,597 ms | 1,656-1,712 ms | |

- The server alone holds 90 MB: fastmcp, uvicorn and the HTTP stack. Phase 2's workers do not shrink
  that, so the 80 MB target needs a lighter server process or a revised target.
- HTTP adds 3-5 ms per call over the shim's socket, and the 16-agent latency is worse than the shim's,
  because one interpreter runs every call and HTTP framing costs more per call. The shim figures here
  are lower than `cli-plan.md`'s (348-419 ms at 16 agents) because the machine was less loaded.
- No call failed in any mode.

## Phase 2: elastic worker pool

1. The HTTP server runs no tool code itself. It sends each tool call to a worker: a plain-Python
   process that imports only `codebase_kg.tools` and the stdlib (about 21 MB, measured).
2. With an idle worker available, the server uses it. With none and fewer than `max_workers`
   running, it starts one (about 150-300 ms). Otherwise the call queues.
3. A worker exits after 60 s with no call. While nothing calls, the pool holds no memory.
4. Calls stay in the server process when `max_workers` is 0. That's the setting for a machine with
   little memory, and the path for tests.
5. Writes go through the CLI (Phase 3), so workers only read, and SQLite serves concurrent readers.

## Phase 2 results

Built as steps 1-5 describe, in `mcp/src/codebase_kg/pool.py` and `worker.py`, with these changes:

| Change | Why |
|---|---|
| The server still runs the six write tools, and resolves each read call's graph before it sends the call. | Writes move to the CLI in Phase 3. The graph comes from the session's binding (roots, headers or shim handshake), which only the server holds; the worker gets the absolute path and never looks for a graph. |
| Shim sessions use the pool too. A private stdio server (the shim's fallback, or `CODEBASE_KG_SHARED=0`) has none. | Shim and HTTP sessions share one server process and one set of tool registrations. A private server serves one session, so it has nothing to spread. |
| A worker is the base interpreter (`sys._base_executable`) with `-I -S`, not the venv's `python`. | On Windows a venv's `python.exe` is a launcher that starts the base interpreter as a second process. The worker imports nothing from the venv. |
| The server, not the worker, ends an idle worker: after 60 s idle it closes the worker's stdin, and the worker exits at EOF. | A worker that timed itself out could exit just as the server sent it a call. Exiting at EOF also ends every worker when the server dies. |
| Each call has a timeout, `CODEBASE_KG_CALL_TIMEOUT`, default 60 s. A worker that misses it is killed; the call returns a tool error. A queued call has no separate timeout. | `kg_validate` walks the source tree and takes seconds on a large repo. A queued call waits at most until the calls ahead of it finish or time out. |
| `max_workers` comes from `CODEBASE_KG_MAX_WORKERS`, else `CLAUDE_PLUGIN_OPTION_MAX_WORKERS`, read once at server start. `GET /health` reports it. | The `SessionStart` hook has the plugin option in its environment, and `shim.spawn_server` passes the environment on, so the hook needs no change. A running server keeps its value; a changed setting applies when the next server starts (after the 8-hour idle exit, a newer build's takeover, or ending the process). A server a shim starts sees no plugin option and uses 4. |
| A worker holds about 24 MB, not 21. | Measured: working set after `kg_stats`, `kg_search`, `kg_neighborhood` and `kg_validate`, median of 8. |

Tests: `mcp/tests/test_pool.py` (grow, queue at the cap, shrink after idle, a crashed worker, a hung
worker, a tool error, a worker that cannot start, `close`, the setting, every read tool equal to the
in-process result through `mcp.call_tool`, error text equal, `max_workers` 0, a worker imports no
fastmcp, mcp, pydantic, starlette or anyio). `test_http_server.py` compares a `max_workers` 0 server
with a pooled one over HTTP.

### Benchmarks

`concurrency.py` gained an `http0` mode (`max_workers` 0) beside `http` (the pool, default 4) and a
`--max-workers` option; both scripts report the whole machine's CPU load. `cli_vs_mcp.py` reads HTTP
memory before the first call, after the calls, and again after `--idle-wait` (70 s). Cartographer's
graph, Windows 11, 16 CPUs. The machine was shared: CPU load before the runs was 66 % and 28 %
(`concurrency.py`), 27 % and 41 % (`cli_vs_mcp.py`), and 29-53 % during them. Ranges cover two runs.

| Measure | Phase 1 HTTP | `http0` (no pool) | `http` (pool of 4) | Target |
|---|---|---|---|---|
| Call median, 1 agent | 14.4-17.2 ms | 17.1-18.5 ms | 16.7-17.4 ms | no slower than 22-25 ms: **met** |
| Call median, 4 agents | 48-51 ms | 60-64 ms | 34-44 ms | |
| Call median, 8 agents | 136-148 ms | 145-152 ms | 83-88 ms | |
| Call median, 16 agents | 306-325 ms | 323-329 ms | 147-161 ms | below 150 ms: **met once, missed once** |
| Slowest call, 16 agents | 1,656-1,712 ms | 1,645-1,672 ms | 520-581 ms | |
| Call median, 16 agents, shim | 222-228 ms | | 81-83 ms | |
| Warm call, one session | 7.8-9.1 ms | | 10.0-12.3 ms | |
| Memory, server alone before any call | | | 82.8 MB, 2 processes | |
| Memory, 1 session, after its calls | 90 MB | | 107.7-107.9 MB, 3 processes | |
| Memory, 8 sessions, after their calls | 94 MB | | 163.3-163.9 MB, 6 processes | below 80 MB: **missed** |
| Memory, 70 s after the last call | | | 91.3-91.8 MB, 2 processes | back to the server's own: **met** |

- **The pool halves the latency under load and removes the long tail.** No call failed in any mode.
- **The HTTP front is the limit now.** With `--max-workers 8` the 16-agent median was 160 ms, no
  better than 4 workers. Over 16 agents' 144 calls, the server process used about 1.5-2.0 s of CPU
  (10-14 ms per call) with the pool, against 9.6 s without it; the benchmark's client used 0.9 s.
  The shim path, with the same pool and cheaper framing, reaches 81-83 ms.
- **A single call costs 2-3 ms more** (a pipe round trip and one JSON parse), still inside the target.
  The first call after the pool shrank pays a worker start: 200-270 ms, median 222 ms.
- **Memory under load is higher than Phase 1** by about 24 MB per busy worker, and falls back once
  the workers idle out. The server keeps about 8 MB it grew while serving (82.8 MB before the first
  call, 91-92 MB after).

### What the server's 83 MB is

Working set as each import is added in order, in one process, venv Python 3.12, two runs within
0.3 MB:

| Import | MB |
|---|---|
| Python start-up | 15.3 |
| `codebase_kg.query` (sqlite3, argparse, the tools) | 6.1 |
| `pydantic` | 2.1 |
| `anyio` | 4.1 |
| `starlette` | 3.4 |
| `uvicorn` | 4.0 |
| `mcp` (its `__init__` imports `mcp.client` and both `mcp.types` and `mcp_types`: the protocol's pydantic models) | 27.9 |
| `fastmcp` | 4.3 |
| `codebase_kg.server` (16 tool registrations, their pydantic schemas) | 9.4 |
| Total, 863 modules | 77.2 |

The `mcp` package is the largest single cost, and most of it is protocol models a server of this size
does not use. An 80 MB target with 8 sessions needs a server that does not import `mcp` and
`fastmcp`, which is a rewrite of the HTTP front, outside Phase 2.

## Lean front

Phase 2 left the server at 83 MB idle, most of it the `mcp` and `fastmcp` imports, and its HTTP front
as the limit under load. A prototype front built on the standard library alone, run against commit
c028aeb, held 25 MB idle and served Claude Code 2.1.293's captured traffic with the same answers. This
section builds it into the server.

### What was built

| Part | Module |
|---|---|
| The tool catalog: the `tools/list` result and the instructions, generated from the fastmcp registrations in `server.py` by `mcp/scripts/gen_catalog.py` | `catalog.json` |
| Argument checks against each `inputSchema`, with pydantic's lax coercion, and one tool call on the pool; writes to one graph one at a time | `core.py` |
| MCP 2026-07-28 over HTTP: `server/discover`, `tools/list`, `tools/call`, the roots `input_required` round trip, the empty lists, 202 for a notification; Host, Origin and token checks; `/health`, `/shutdown`; port claim and verified handover; 8 MB body cap; chunked bodies; 60 s keep-alive idle timeout | `http_transport.py` |
| Classic MCP over lines: the shims' TCP handshake and session, and the private stdio server | `tcp_transport.py` |
| The process: state file written private, idle exit (8 h for the HTTP holder), orphan exit within 5 s, `--serve` or private | `daemon.py` |
| Graph-path resolution, shared by the server and `server.py` | `resolve.py` |
| The six write tools, beside the read tools | `worker.py` |
| `kg_cli.py server status` and `server stop` | `control.py` |

The server is the base interpreter running `daemon.py` with `-I -S` (`shim.server_command`). It
imports no third-party package, so the plugin no longer builds a venv to run it. `fastmcp` moved to
the dev dependencies: `server.py` keeps the tool definitions for the catalog and for the tests that
compare fastmcp's handling of a call with the core's.

### Deviations

| Change | Why |
|---|---|
| The fastmcp runtime path is gone: no `uv run` launch, no server venv (`venv-<key>`, `CODEBASE_KG_DATA_DIR`), no uvicorn or starlette. The private fallback server is the same stdlib server on stdio. | Nothing at runtime needed fastmcp once the private server ran on the core. |
| The writes run on the workers, not in the server. `core.Core` holds one lock per graph path around each write. | The always-on process imports no `edits` module, and a write still never runs beside another write to the same file. `edits` replaces the file from a copy, so two at once would lose one. |
| `max_workers` defaults to 8 and has no upper limit. | The setting's default and `pool.DEFAULT_MAX_WORKERS` must agree: Claude Code 2.1.293 exports no `CLAUDE_PLUGIN_OPTION_*` for a setting the user never set (checked by dumping the hook's environment), so the server falls back to the code default. |
| The headers helper waits up to 3 s for a server while nothing listens on the port. | The server now starts in 0.2 s, inside the helper's 0.3 s run, so a connect attempt could send no token to a server that had just come up. Claude Code 2.1.293 answered that 403 by marking the server "needs auth" in `~/.claude/mcp-needs-auth-cache.json` and stopped connecting, for later sessions too. Found in the live check; the slow fastmcp start never hit it. |
| `kg_cli.py server status` and `server stop`. | A changed `max_workers` applies when the server next starts; `stop` is the way to make that happen without finding the pid. |
| The catalog drops fastmcp's own `_meta` (`{"fastmcp": {"tags": []}}`). | Claude Code ignores it, and it named a package the server no longer has. |
| The server's own request errors (a client that resets its connection) do not print a traceback to the log. | The live check's log filled with `ConnectionResetError` from Claude Code closing keep-alive sockets. |

### Results

Measured 2026-10-07 on Windows 11, 16 CPUs, cartographer's graph (1.14 MB), `max_workers` 8,
`concurrency.py --modes http` and `cli_vs_mcp.py --modes http`, each twice; ranges cover both runs.
The machine was shared: CPU load before the runs was 22-47 %. "Phase 2" is the fastmcp server with a
pool of 4, from the Phase 2 results above.

| Measure | Phase 2 | Prototype (pool of 4) | Lean front (pool of 8) |
|---|---|---|---|
| Server start, spawn to state file | 2.2-2.4 s | 0.16-0.18 s | 0.18 s (4 starts) |
| Memory, server alone before any call | 82.8 MB, 2 processes | 25.2 MB, 1 process | 26.3-26.4 MB, 1 process |
| Memory, 1 session, after its calls | 107.7-107.9 MB, 3 processes | 47.3 MB, 2 processes | 47.0-47.1 MB, 2 processes |
| Memory, 8 sessions, after their calls | 163.3-163.9 MB, 6 processes | 110.8 MB, 5 processes | 170.3-189.7 MB, 8-9 processes |
| Memory, 70 s after the last call | 91.3-91.8 MB, 2 processes | 26.2 MB, 1 process | 26.8-27.0 MB, 1 process |
| Connect | 101 ms (Phase 1) | | 69-73 ms |
| Warm call, one session (`kg_search`, `kg_node`, `kg_neighborhood`) | 10.0-12.3 ms | | 3.7-6.2 ms |
| Call median, 1 agent | 16.7-17.4 ms | 5-7 ms | 7.8-8.9 ms |
| Call median, 4 agents | 34-44 ms | | 12.8-13.9 ms |
| Call median, 8 agents | 83-88 ms | | 24.9-29.6 ms |
| Call median, 16 agents | 147-161 ms | 48-66 ms | 80.6-115.0 ms |
| Slowest call, 16 agents | 520-581 ms | | 382-499 ms |
| CPU load during the 16-agent run | 29-53 % | | 47-67 % |
| Calls that failed | 0 | 0 | 0 |

- **Memory meets the 80 MB target while idle and with one session**, and falls back to 27 MB once
  the workers idle out. With 8 sessions calling at once, 8 workers start, so the total is about
  26 MB plus 20-21 MB per worker. A pool of 4 holds that peak near 110 MB.
- **The 16-agent median is below the 150 ms target in both runs.** It is higher than the
  prototype's, with the machine more loaded. One run with `--max-workers 4` at 13 % load before it
  gave medians of 8.1, 9.8, 19.8 and 49.9 ms at 1, 4, 8 and 16 agents; on this machine 8 workers
  did not beat 4. The default stays 8, as decided, and the setting has no upper limit.
- The first call after the pool shrank pays a worker start: 149-173 ms.

### Verification

- Tests: `test_http_battery.py` turns the prototype's 44-case battery into permanent cases (status
  codes and JSON shapes). `test_catalog.py` checks the catalog against the registrations and 79
  argument cases against fastmcp's validation. `test_pool.py` checks every read and write tool's
  result from a worker, from the core without a pool, and from fastmcp, and that concurrent writes
  to one graph all land. The shim tests run classic `initialize`, `tools/list` and `tools/call`
  through the unchanged shim.
- Live: a copy of this repo renamed `kgx`, on port 47890, loaded with `claude -p --plugin-dir
  --model haiku` in sentinel-swarm. The hook started the server cold, Claude Code connected, answered
  the roots request, and `kg_search` for "ledger" returned `ledger_core` first.

## Phase 3: start and end tools to the CLI

1. `kg_cli.py query` already runs `kg_stats`, `kg_validate` and `kg_parity_gaps`. Add `kg_cli.py edit
   <tool> [json-args]` for the six write tools, over the same `edits.py` functions.
2. Update the `refresh`, `validate`, `audit`, `link` and `build` skills to call the CLI by
   `"${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py"`. Update `hooks/kg_search_gate.py` where it names
   MCP tools.
3. Remove those nine tools from the MCP registration for Claude Code. The stdio shim keeps all 16
   until its clients move.
4. **Cross-repo:** sentinel-swarm's role templates list `mcp__codebase-kg__kg_stats` and
   `kg_validate`. They keep working through the shim, which keeps all 16 tools. Moving them is
   sentinel-swarm's change, not this one.

## Tests

- Unit: header and Origin checks, token check, port-in-use and older-build handover, per-session
  graph binding over HTTP, pool grow, shrink and queue, `max_workers` 0, the `edit` CLI.
- Parity: every lookup returns the same JSON over HTTP, over the shim and from the CLI.
- Benchmarks: rerun `mcp/bench/cli_vs_mcp.py`, `loop10.py` and `concurrency.py` with an HTTP mode.
  Success means:
  - memory with 8 sessions below 80 MB, against 292 MB today;
  - median call at 16 agents below 150 ms, against 348-419 ms today;
  - one agent's median call no slower than today's (22-25 ms).
- Evals: run the suite on a clean copy without `mcp/.venv`, which holds hard links the eval runner
  refuses. Compare with the 5-of-6 baseline; `no-graph-negative` already fails on this branch before
  any change.
- `claude plugin validate --strict` on the repo and on `.claude-plugin/plugin.json`.

## Rollout

| Step | Change | Ships |
|---|---|---|
| 0 | Phase 0 checks, answers recorded here | Docs |
| 1 | HTTP transport, security, health, start hook, `userConfig`, `.mcp.json` | 0.12.0 |
| 2 | Worker pool | 0.12.0 |
| 2b | Lean front: stdlib server, no venv, writes on the workers | 0.12.0 |
| 3 | CLI `edit`, skills on the CLI, nine tools off the HTTP registration | 0.13.0 |

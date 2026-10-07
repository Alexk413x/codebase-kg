# CLI in place of MCP: codebase-kg

Status: proposal, not started. Written 2026-10-07.

## Finding

All 16 tools can run as CLI commands and return the same results. The server holds no state a
CLI can't rebuild. Tools: `kg_search`, `kg_node`, `kg_neighborhood`, `kg_find_by_kind`,
`kg_find_by_path`, `kg_find_by_link`, `kg_find_by_reference`, `kg_parity_gaps`, `kg_stats`,
`kg_validate`, `kg_upsert_node`, `kg_delete_node`, `kg_add_link`, `kg_remove_link`,
`kg_add_reference`, `kg_remove_reference`.

The server already runs as one shared daemon per machine (`mcp/src/codebase_kg/daemon.py`,
`shim.py`). Each session pays only for the shim relay and the tool schemas in context.

## What changes without the server

| Behavior | With MCP | With a per-call CLI |
|---|---|---|
| Graph load | Once per daemon, cached | Once per call, unless the CLI goes through the daemon |
| Process start | Once | Every call: `uv run --project … --frozen --no-sync` plus Python imports. Not measured. |
| Tool schemas in context | 16 schemas, or deferred | None. A skill documents the commands. |
| Permissions | Per-tool allow rules | `Bash(codebase-kg:*)` allow rule |
| Errors | Structured tool error | JSON on stdout and a non-zero exit code |

## Solution

1. Add a `codebase-kg` CLI entry point with one subcommand per tool. Each subcommand calls the
   same core function the tool calls.
2. Read arguments as JSON on stdin (`codebase-kg search < args.json`) and accept simple flags for
   single-string arguments. Windows argv quoting breaks nested JSON.
3. Print the tool's return value as JSON. Exit non-zero on error and print the error as JSON.
4. Route the CLI through the daemon when one is running, and fall back to in-process. That keeps the
   graph cache warm and makes CLI and MCP results come from the same process.
5. Run one parity test suite against both front ends: same input, byte-identical JSON output.
6. Update the skills to call `"${CLAUDE_PLUGIN_ROOT}/…"` CLI paths, and add the Bash allow rule.
7. Keep the MCP server until the CLI passes the parity suite and evals. Then remove it from
   `.mcp.json`.

## Measured baseline

Measured 2026-10-07 on Windows 11 with `mcp/bench/cli_vs_mcp.py` against two graphs:
sentinel-swarm (0.27 MB) and cartographer (1.14 MB). The prototype CLI is `mcp/launch/kg_cli.py query
search|node|neighborhood`, run by system Python with no venv. For all three tools, the CLI's JSON equals
the MCP tool's structured result on both graphs.

| Measure | CLI | MCP |
|---|---|---|
| Call, median | 130-260 ms (system Python), 200-250 ms (`uv run --no-project`) | 5-9 ms warm |
| Session start | None | 2.5-2.6 s to connect, then 0.5-0.7 s for the first call |
| Memory while idle | 0 | 116 MB for one session (4 processes), 292 MB for 8 sessions (18 processes) |
| Memory per call | 21 MB peak, released when the call ends | Within the figures above |
| 8 calls at once | 270-300 ms wall | 570-670 ms for each session's first call, 23-49 ms once warm |

Reading the numbers:

- A CLI call costs about 0.2 s more than a warm MCP call. That is small next to a model turn.
- MCP pays about 3 s per session before its first answer. Claude Code connects servers while the
  session starts, so the user rarely waits for the connect. The first call's 0.5-0.7 s does land on a
  turn. Each benchmark run also started a fresh daemon, because the dev tree's build differs from the
  installed plugin's, so the connect figure is the cold case.
- MCP holds about 12-15 MB per session plus the shared daemon, all the time. The CLI holds nothing
  between calls.
- No errors or slowdowns at 8 concurrent CLI calls.

The MCP memory figures count only processes the benchmark started. Live sessions' servers are excluded.

## Ten-call loop

`mcp/bench/loop10.py` runs ten different read calls in a loop, five rounds, four ways, against
cartographer's graph. Measured 2026-10-07 on Windows 11. All four modes return identical JSON for all
ten calls.

| Call | In-process | CLI | MCP (warm) | CLI + server |
|---|---|---|---|---|
| `kg_search` (2 queries) | 4-5 ms | 171-182 ms | 9 ms | 152 ms |
| `kg_node` (2 ids) | 2 ms | 159-183 ms | 6 ms | 142-143 ms |
| `kg_neighborhood` depth 1, depth 2 | 3-7 ms | 163-187 ms | 8-14 ms | 147-169 ms |
| `kg_find_by_kind`, `kg_find_by_path` | 3-4 ms | 168-175 ms | 7-8 ms | 140-158 ms |
| `kg_stats` | 111 ms | 253 ms | 98 ms | 235 ms |
| `kg_validate` | 1,982 ms | 2,415 ms | 2,170 ms | 2,248 ms |
| **Loop of 10, median** | **2.1 s** | **4.2 s** | **2.4 s** | **3.6 s** |

Memory during the loop (3 rounds, 30 calls per mode):

| Mode | While idle | During the loop |
|---|---|---|
| CLI | 0 | 21 MB peak per call (max 25 MB), freed when the call ends |
| CLI + server | The shared server stays resident | 20 MB peak per client call, plus the server |
| MCP, one session | 116 MB (shim, `uv`, launcher and server processes) | 123 MB after 30 calls |
| One plain Python process making all 30 calls | 0 | 21 MB after imports, 29 MB peak |

The live shared server on this machine held 37.5 MB, and each session's shim 12-22 MB. Most of the
MCP figure is process overhead and the fastmcp import, not the graph: plain Python with the same
imports and calls peaks at 29 MB.

- "In-process" is the function called with no process start: the floor for any design.
- "CLI + server" is `mcp/bench/kg_client.py`: one Python process per call that forwards to the shared
  server. It saves only 15-40 ms per call over the plain CLI, because Python start-up (about 140 ms on
  this machine) stays. It isn't worth shipping in this form.
- `kg_validate` takes about 2 s in every mode, so the mode adds 10-20 % at most. For quick lookups
  the CLI adds about 160 ms each.

## Many agents at once

`mcp/bench/concurrency.py` runs N agents at once. Each agent makes the nine lookup calls of the loop in
order (`kg_validate` left out). Each MCP agent has its own session through the shim to the one shared
server. Each CLI agent starts one process per call. Measured 2026-10-07 on a 16-CPU Windows 11 machine
at 94 % CPU load from other work, so treat the figures as a range. Two runs, no errors in either.

| Agents | CLI, call median | CLI, all agents done | MCP, call median | MCP, all agents done |
|---|---|---|---|---|
| 1 | 177-570 ms | 1.8-5.8 s | 22-25 ms | 0.5 s |
| 4 | 434-692 ms | 4.2-6.5 s | 33-64 ms | 0.6-1.2 s |
| 8 | 333-635 ms | 3.4-6.3 s | 162-201 ms | 2.6-2.7 s |
| 16 | 659-968 ms | 7.9-11.2 s | 348-419 ms | 4.8-6.3 s |

- Reads don't conflict. SQLite serves concurrent readers, and no call failed.
- MCP slows sharply past 4 agents. All sessions share one server process. The latency growth suggests
  it runs about one tool call at a time; that is inferred, not confirmed in the server code. At 16
  agents a call waits about 0.4 s, and the slowest wait 2-2.5 s.
- The CLI slows less in proportion, because each call is its own process on its own CPU. It starts
  slower, so it stays behind MCP at every N measured.
- Concurrent graph writes weren't tested.

## Split by when a tool runs

Keep MCP for tools the model calls repeatedly while it works, where 5-10 ms against 160 ms matters,
and especially if a fast local model makes the calls. Move tools that run once, in a fixed order, at
the start or end of a task to the CLI, called by a skill or a hook.

| Group | Tools | Runs | Interface |
|---|---|---|---|
| Lookups while working | `kg_search`, `kg_node`, `kg_neighborhood`, `kg_find_by_kind`, `kg_find_by_path`, `kg_find_by_link`, `kg_find_by_reference` | Many times, interleaved with edits | MCP |
| Start or end of a task | `kg_stats`, `kg_validate`, `kg_parity_gaps` | Once, at session start or before a push | CLI from a hook or skill |
| Graph writes after edits | `kg_upsert_node`, `kg_delete_node`, `kg_add_link`, `kg_remove_link`, `kg_add_reference`, `kg_remove_reference` | Once, at the end of a change | CLI from the refresh skill |

What the split saves, and what it doesn't:

- **Context:** 9 of 16 tool schemas leave the session.
- **Memory: nothing, while any tool stays on MCP.** One remaining MCP tool keeps the per-session shim
  (about 12 MB) and the shared server running. Memory falls only when a plugin's whole server goes,
  so the split saves memory only in plugins whose every tool runs at start or end.
- **Speed:** the start and end tools lose 0.2-0.4 s, once per task.

## Open questions

- Decide whether the existing hooks (`SessionStart`, `PreToolUse`, `PostToolUse`) call the CLI
  directly instead of reading the graph themselves.

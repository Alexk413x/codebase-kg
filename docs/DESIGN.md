# Design — locked decisions, resolved questions, principles

The durable "why" behind the plugin. `SCHEMA.md` is the *what*; this is the rationale and the
settled calls.

## Locked design decisions

These are locked — change them only with a deliberate reason.

1. **Per-repo graph.** One `code_graph.db` per codebase, identified by where it lives, not a
   platform-prefixed filename. The iOS graph lives in the iOS repo; the Android graph in the Android
   repo. Generic across repos.
2. **Committed artifact.** The graph is checked in and reflects the code's current state. A
   generated-on-demand, gitignored cache was proposed and rejected: the point of the artifact is
   that it travels with the code and everyone sees the same map.
3. **Generic node schema** (language-agnostic): `id / kind / description / anchors / edges /
   section / parity / counterpart / divergence`. See `SCHEMA.md` §4.
4. **Symbol-based anchors** (`path#Symbol`), **never line numbers.** Line numbers rot on every edit
   (the #1 drift source) and barely help an agent that has `Grep`. Symbols are stable, greppable,
   and universal across languages. The store rejects a numeric symbol outright.
5. **Source-derived, not history-derived.** The graph reflects code as it stands. Tickets, dates and
   change narrative belong to git and the tracker — `SCHEMA.md` §5.
6. **No separate parity file.** A node cross-links *directly* to its counterpart node in the other
   repo's graph, carrying a `parity` status + one-line `divergence`. Detail stays on each side;
   "find all gaps" = `kg_parity_gaps`.
7. **Point, don't copy.** Never paste code into the graph — reference symbols.
8. **Comprehensive updates.** Every refresh updates *all* affected nodes (add new, edit changed,
   remove deleted) + edges.
9. **Advisory, never blocking.** Freshness, drift and validation surface as advice; they never gate
   a commit, build, or tool.
10. **Constraints over checks.** Anything expressible as a constraint in the store is one, so bad
    data fails to be written rather than being found later. See "Why a database" below.
11. **Suggest, never wire.** Nothing in the plugin writes a repo's git config on the user's behalf.
    Git leaves `.git/config` out of a clone deliberately, so that cloning a repo cannot cause it to
    execute code; `core.hooksPath` is the switch that makes a repo's committed `.githooks/*.py` run.
    Setting it for someone would route around that protection and make vendored scripts live in a
    fresh clone without anyone choosing to run them. The plugin therefore ships `install.sh` as a
    committed, per-clone command a person runs, and the `SessionStart` hook only names it. A plugin
    the user installed may suggest; it may not decide.

## Resolved open questions

| Question | Decision | Note |
|---|---|---|
| **Storage format** | Committed SQLite (`code_graph.db`) | Constant-time open, write-time integrity, persisted FTS5. Supersedes "Markdown-as-source". |
| **Human readability** | Explicitly a non-goal | The artifact is AI-consumed. It is not read raw and not reviewed in diffs. |
| **Plugin name** | `codebase-kg` | Unchanged **on purpose** — see "The rename we didn't do". Slash `/codebase-kg:query\|build\|refresh\|audit\|link\|validate` (skills) + `setup` (command); MCP server `codebase-kg`. |
| **MCP tool names** | `kg_*` | Unchanged, same reason. |
| **Repo layout** | Standalone plugin at root | `plugin.json` at root + a thin `marketplace.json` so it installs. |
| **Graph location** | Per-repo `knowledge/code_graph.db` | Always `knowledge/` (no repo-root fallback); override per clone via `graph_path` in `.local.md`. |
| **Refresh engine** | Agent-driven first | The agent reads source and emits nodes — keeps it language-agnostic. Add static parsers later for speed; never as the only path. |
| **Search index** | FTS5, persisted in the file | An in-memory index was deferred because building it landed on the load path. Persisting it removes that objection entirely: built once at write time, free on open. |
| **Counterpart direction** | Reciprocal | Both sides link; `validate` flags one-directional or dangling links. |
| **Freshness gate** | Content check, advisory | Replaced the date-based push block. See "The gate that measured the wrong thing". |

## Why a database

Three properties, in the order they mattered:

**Integrity you cannot opt out of.** Foreign keys make a dangling edge unwritable. The primary key
makes a duplicate id unwritable. CHECK constraints encode the three legal parity shapes, so a
half-filled parity triple is a failed write rather than a validation finding. A transactional build
means a regeneration lands whole or not at all — there is no truncated-file state. This moved most
of `kg_validate`'s old job from "report afterwards" to "cannot happen", and what remains in the
report is only what a file genuinely cannot know about itself: whether its anchors still point at
real code, whether anything in scope went unmapped, and whether the source moved after the
description was written.

That last pair arrived in v3, after a gap this section had glossed over. Reporting "which files does
no node cover?" by *inferring* the source extensions from what was already anchored is silent for a
category with zero coverage — the graph could describe none of a repo's build configuration and
nothing would say so. Scope is now declared (`meta.covers`), so every file lands in a named bucket
and there is no invisible state. And a resolving anchor was never evidence that a *description* was
still true, so the `source` table records what the code looked like at build time; a refactor that
keeps a name and replaces the body now shows up.

**Load cost that stops growing.** Parsing Markdown was O(nodes) and paid on every cold load —
~12 ms at 175 nodes, ~820 ms at 10,500. Opening the store is ~1 ms at any size. That is a change of
scaling class, not a constant-factor win, and it is what makes a large monorepo viable.

**Search that is actually indexed.** FTS5 lives in the file, so the index costs nothing to load.
Ranked search over 175 nodes runs in ~0.6 ms including hydration.

Not a reason: token efficiency. Measured, the container is ~3% of the payload; content dominates.
Nor file size — the store is larger on disk than the Markdown was (~390 KB vs ~115 KB for 175
nodes), which is an honest cost of carrying an index, and git packs it fine.

## The rename we didn't do

The artifact is a map of code structure, so the file and schema were renamed
(`KNOWLEDGE_GRAPH.md` → `code_graph.db`, `summary` → `description`). The rename **stops there**.

The MCP tools stay `kg_*` and the plugin stays `codebase-kg`, because renaming them buys nothing and
costs a working install. Precedent: the `a11y-plugin` → `accessibility-tools` marketplace rename
left Acme-iOS pointing at a nonexistent marketplace, silently disabling two plugins until it was
found weeks later. Renaming `codebase-kg` would repeat that across three repos plus user settings.

## One server, two transports

A server per session cost about 116 MB and four processes for each open session. One
`codebase-kg --serve` process per machine and build now serves every session, over two transports
in the same process with the same tool registrations:

- **Streamable HTTP for Claude Code**, at `http://127.0.0.1:<server_port>/mcp`. The server holds
  about 90 MB however many sessions connect, plus about 24 MB per busy worker, against 317 MB for
  eight shim sessions (`http-server-plan.md`).
- **A loopback TCP socket for stdio clients**, through `kg-shim`, which Codex and sentinel-swarm's
  role sessions launch. Unchanged.

The server has no cwd of its own that means anything, so each session tells it which repo it is in.
A shim sends its cwd in a handshake. Claude Code speaks MCP 2026-07-28 over HTTP, which has no
sessions, and its `headersHelper` runs in the plugin folder, so neither can carry the cwd. Instead
the helper sends a random client id, and the server answers that client's first tool call with an
`InputRequiredResult` asking for roots. Claude Code answers with the session's cwd; the server caches
it under the client id. Measured: the extra round trip costs about 10 ms, once per connection.

Two rules follow from Claude Code's behaviour, measured in `http-server-plan.md` (Phase 0):

- **Something must listen within about 7 s of session start.** Claude Code gives up on a server it
  cannot reach by then, for the whole session, and `SessionStart` hooks do not run before it first
  tries. The `SessionStart` hook starts the server; Claude Code retries as the hooks finish.
- **The server must outlive the sessions.** Claude Code sends each call to whatever holds the port
  and never re-runs anything that could start a server mid-session. So a server that holds the HTTP
  port exits only after 8 hours with no request.

The bearer token is one per user and outlives each server, because Claude Code keeps the headers it
got at connect and sends them to a server that restarted since. It is sent only to a server whose
pid and port match a state file only this user can read, and a takeover authenticates with the old
server's own state-file token, so a program squatting on the port never receives a credential.

### Read tools on a worker pool

One interpreter running every session's calls made 16 agents at once wait about 320 ms per call.
So the shared server sends each read tool call to a worker process (`pool.py`, `worker.py`), for
HTTP and shim sessions alike:

- **A worker imports no MCP code.** It is the base Python interpreter, run with `-I -S`, importing
  only `query`, `tools` and the standard library: about 24 MB, against the server's 83 MB. It
  dispatches through `query.TOOLS`, the table `kg_cli.py query` uses, so the CLI, a worker and the
  in-process server return the same JSON.
- **The server resolves the graph, never the worker.** The server binds each call to its session as
  before, resolves the graph path, and sends the absolute path with the call. The worker opens the
  graph for that call and closes it, as the server does.
- **Elastic.** A call takes an idle worker, else starts one (about 200-270 ms) while fewer than
  `max_workers` run, else waits. A worker idle for 60 s is told to exit, so an idle server holds only
  its own process. Requests and replies are one JSON line each on the worker's stdin and stdout; a
  worker exits at stdin EOF, so it never outlives the server.
- **Failure stays with one call.** A worker that exits during a call, or exceeds the call timeout,
  is killed and dropped; that call returns a tool error and the next call starts a fresh worker.
- **Writes stay in the server**, and `max_workers` 0 runs every call there.

Measured (`http-server-plan.md`, Phase 2): with 16 agents the HTTP median fell from 323-329 ms to
147-161 ms, and 8 workers did no better than 4. What remains is the server's HTTP front, about
10-14 ms of CPU per call in one interpreter.

## The gate that measured the wrong thing

The pre-push hook used to block a push when source changed and the graph's `refreshed:` header was
not today's date. It was removed, for two independent reasons:

1. **It contradicted principle #9.** A hard block is exactly the thing the plugin says it never does.
2. **A date cannot measure freshness.** It proves someone edited the file, not that the nodes match
   the code. The evidence was in the live graph: the Android header read `refreshed: 2026-07-12`
   while three nodes were stale from a commit that landed after it — under a gate designed to
   prevent precisely that.

What replaced it asks questions with real answers: does every anchor still resolve to code, and does
any source file in this push have no node covering it. Both are checkable, neither needs a date, and
the check reports rather than blocks.

## The verification that could only pass

`/codebase-kg:setup` step 9 used to confirm the textconv driver by running `git diff` in the shell
that had run `git config` one line earlier. That is a test of a value it had just set, and it passes
no matter how machine-local the value is — which is how the command reported "graph diffs are live"
in a repo where the configured driver was a version-stamped path under one developer's plugin cache
(`…/.claude/plugins/cache/codebase-kg/codebase-kg/0.5.2/mcp`), and every other clone silently showed
`Binary files differ`. Git reports nothing when a textconv command does not exist; it just falls back.

Two rules came out of it, and both are in the command now:

- **A wiring check runs where the wiring will be met.** Setup verifies from a fresh clone with its
  own empty `.git/config`, so the only thing that can make it pass is the committed installer.
- **Anything written into `.git/config` must mean the same thing on every machine.** The textconv
  command is a tag-pinned remote, never `${CLAUDE_PLUGIN_ROOT}` — a tag, because this runs on every
  diff of the graph and must not change under the repo silently.

## Genericity rules (do not violate)

- **No language hardcoding** anywhere in skills/MCP. Source reading is agent-driven → handles any
  language. `kind` is free text. Even "which files count as source" is derived from the extensions
  the graph already anchors on.
- **Symbol discovery via Grep always**; ctags/LSP only when present — never assume a toolchain.
- **All per-repo specifics live in config** (`SCHEMA.md` §8), never in the generic components.

## Principles carried from the author's existing plugins

- **Advisory, never blocking** (the a11y-plugin rule — hard gates cause workarounds).
- **Source over tickets** — the graph is a code mirror, verified against source.
- **Comprehensive updates** — a partial refresh is what causes drift.

## Dogfood target

A real **iOS ↔ Android** app pair, plus a backend. Known cross-codebase cases to reproduce (modeled
in `docs/examples/`): matched (SavedArticle ↔ SavedArticleEntity), divergent
(PersonalizedRankingService ↔ FeedRanker), android-only (a night-theme feature iOS has only in a
product doc).

Parity is expressed by **direct node cross-linking** (decision #6, `SCHEMA.md` §9) — there is no
separate parity file. "Find all gaps" is the `kg_parity_gaps` query across both graphs.

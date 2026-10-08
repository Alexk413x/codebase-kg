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
   "find all gaps" = the `kg_parity_gaps` query, which runs through `mcp/launch/kg_cli.py query`.
7. **Point, don't copy.** Never paste code into the graph — reference symbols.
8. **Comprehensive updates.** Every refresh updates *all* affected nodes (add new, edit changed,
   remove deleted) + edges.
9. **Advisory, with one gate.** Freshness, drift and validation surface as advice; they never gate
   a commit or a build. The push check blocks on exactly one thing: a mapped file whose content no
   longer matches the digest recorded when the graph was built. See "The gate that measured the
   wrong thing".
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
| **Plugin name** | `codebase-kg` | Unchanged **on purpose** — see "The rename we didn't do". Skills `/codebase-kg:query\|build\|refresh\|audit\|link\|validate`, plus `setup`, which only the user invokes; MCP server `codebase-kg`. |
| **MCP tool names** | `kg_*` | Unchanged, same reason. |
| **Repo layout** | Standalone plugin at root | `.claude-plugin/plugin.json` + a thin `.claude-plugin/marketplace.json` so it installs from this repo. |
| **Graph location** | Per-repo `knowledge/code_graph.db` | Always `knowledge/` (no repo-root fallback); override per clone via `graph_path` in `.local.md`. |
| **Refresh engine** | Agent-driven first | The agent reads source and emits nodes — keeps it language-agnostic. Add static parsers later for speed; never as the only path. |
| **Search index** | FTS5, persisted in the file | An in-memory index was deferred because building it landed on the load path. Persisting it removes that objection entirely: built once at write time, free on open. |
| **Counterpart direction** | Reciprocal | Both sides link; `validate` flags one-directional or dangling links. |
| **Freshness gate** | Content check | Pushes block only on stale mapped files; everything else is advice. Replaced the date-based push block. See "The gate that measured the wrong thing". |

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

A server per session cost about 116 MB and four processes for each open session. One server
process per machine and build now serves every session, over two transports in the same process,
from one tool catalog and one call path (`core.py`):

- **MCP 2026-07-28 over HTTP for Claude Code**, at `http://127.0.0.1:<server_port>/mcp`. The
  server holds about 26 MB however many sessions connect, plus about 21 MB per busy worker, against
  317 MB for eight shim sessions (measured 2026-10-07 with `mcp/bench/`).
- **Classic MCP over a loopback TCP socket for stdio clients**, through `mcp/launch/kg-shim`, which
  Codex and sentinel-swarm's role sessions launch.

### A server on the standard library

The server imports only the standard library and runs on the base Python with `-I -S`. fastmcp,
the `mcp` package and the HTTP stack beneath them made up about 55 of its 83 MB and 2 s of its
start, for a protocol surface of a few methods. So the tool definitions stay fastmcp registrations
in `server.py`, which nothing at runtime imports, and `mcp/scripts/gen_catalog.py` writes them to
`catalog.json`. The
server sends that catalog and checks arguments against it with pydantic's lax rules. Two tests guard
the split: the catalog must equal what the registrations produce, and each of 79 argument cases
must pass, fail or coerce in the core as it does in fastmcp.

With no third-party import there is no venv to build on the first start after an update, which
used to outlast Claude Code's 7 s connect window. The server starts in about 0.2 s.

The server has no cwd of its own that means anything, so each session tells it which repo it is in.
A shim sends its cwd in a handshake. Claude Code speaks MCP 2026-07-28 over HTTP, which has no
sessions, and its `headersHelper` runs in the plugin folder, so neither can carry the cwd. Instead
the helper sends a random client id, and the server answers that client's first tool call with an
`InputRequiredResult` asking for roots. Claude Code answers with the session's cwd; the server caches
it under the client id. Measured: the extra round trip costs about 10 ms, once per connection.

Two rules follow from Claude Code's behaviour, measured against Claude Code 2.1.293 on 2026-10-07:

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

### Tool calls on a worker pool

One interpreter running every session's calls made 16 agents at once wait about 320 ms per call.
So the shared server sends each tool call to a worker process (`pool.py`, `worker.py`), for HTTP and
shim sessions alike:

- **A worker is the base Python interpreter, run with `-I -S`.** A worker that only reads imports
  `query`, `tools` and the standard library, about 21 MB; `edits` loads on its first write. Reads
  dispatch through `query.TOOLS`, the table `kg_cli.py query` uses, so the CLI, a worker and the
  in-process path return the same JSON.
- **The server resolves the graph, never the worker.** The server binds each call to its session as
  before, resolves the graph path, and sends the absolute path with the call. The worker opens the
  graph for that call and closes it, as the server does.
- **Elastic.** A call takes an idle worker, else starts one (about 200-270 ms) while fewer than
  `max_workers` run, else waits. A worker idle for 60 s is told to exit, so an idle server holds only
  its own process. Requests and replies are one JSON line each on the worker's stdin and stdout; a
  worker exits at stdin EOF, so it never outlives the server.
- **Failure stays with one call.** A worker that exits during a call, or exceeds the call timeout,
  is killed and dropped; that call returns a tool error and the next call starts a fresh worker.
- **One writer per graph.** Writes run on workers too, but the server holds a lock per graph path
  around each one. `edits` replaces the file from a private copy, so two writes at once would lose
  one. A refused write comes back as `{ok: false, written: false, error}` with `isError`, as before.
- `max_workers` (default 8, no upper limit) caps the pool; 0 runs every call in the server.

Measured with `mcp/bench/concurrency.py` on 2026-10-07: with 16 agents the HTTP median fell from 323-329 ms with one
interpreter to 147-161 ms with the pool behind fastmcp, and to 81-115 ms with the stdlib front.

## The gate that measured the wrong thing

The first pre-push gate blocked a push when source changed and the graph's `refreshed:` header was
not today's date. A date cannot measure freshness. It proves someone edited the file, not that the
nodes match the code. The evidence was in the live graph: the Android header read
`refreshed: 2026-07-12` while three nodes were stale from a commit that landed after it, under a gate
designed to prevent exactly that.

The check that replaced it asks questions with real answers. It compares each mapped file's content
with the digest the graph recorded when it was built, and it names changed files that no node
anchors and deleted files that a node still anchors. None of these needs a date.

The push check blocks on one finding only: a mapped file whose content no longer matches its
digest, including files the push touches. A push publishes the code, so the graph has to match it
first. Before it blocks, `git-hooks/kg_pre_push.py` can run `/codebase-kg:refresh` headless and
commit the refreshed graph. The `PreToolUse` hook `hooks/kg_push_gate.py` denies an agent's
`git push` on the same finding. `KG_STALE_ACK=<n>` and `SKIP_KG=1` pass both. Unmapped, deleted and
drifted files in the pushed commits stay advice, and so does the whole pre-commit check.

## The verification that could only pass

`/codebase-kg:setup` step 9 used to confirm the textconv driver by running `git diff` in the shell
that had run `git config` one line earlier. That is a test of a value it had just set, and it passes
no matter how machine-local the value is — which is how the command reported "graph diffs are live"
in a repo where the configured driver was a version-stamped path under one developer's plugin cache
(`…/.claude/plugins/cache/codebase-kg/codebase-kg/0.5.2/mcp`), and every other clone silently showed
`Binary files differ`. Git reports nothing when a textconv command does not exist; it just falls back.

Two rules came out of it, and the `setup` skill follows both:

- **A wiring check runs where the wiring will be met.** Setup verifies from a fresh clone with its
  own empty `.git/config`, so the only thing that can make it pass is the committed installer.
- **Anything written into `.git/config` must mean the same thing on every machine.** The textconv
  command is a tag-pinned remote, never `${CLAUDE_PLUGIN_ROOT}` — a tag, because this runs on every
  diff of the graph and must not change under the repo silently.

## Genericity rules (do not violate)

- **No language hardcoding** anywhere in skills/MCP. Source reading is agent-driven → handles any
  language. `kind` is free text. Which files count as source is declared per graph in `meta.covers`
  (`SCHEMA.md`), never a built-in list of extensions.
- **Symbol discovery via Grep always**; ctags/LSP only when present — never assume a toolchain.
- **All per-repo specifics live in config** (`SCHEMA.md` §8), never in the generic components.

## Principles carried from the author's existing plugins

- **Advisory by default** (the a11y-plugin rule — hard gates cause workarounds). The one gate is
  the push check on stale mapped files.
- **Source over tickets** — the graph is a code mirror, verified against source.
- **Comprehensive updates** — a partial refresh is what causes drift.

## Dogfood target

A real **iOS ↔ Android** app pair, plus a backend. Known cross-codebase cases to reproduce (modeled
in `docs/examples/`): matched (SavedArticle ↔ SavedArticleEntity), divergent
(PersonalizedRankingService ↔ FeedRanker), android-only (a night-theme feature iOS has only in a
product doc).

Parity is expressed by **direct node cross-linking** (decision #6, `SCHEMA.md` §9) — there is no
separate parity file. "Find all gaps" is the `kg_parity_gaps` query (`kg_cli.py query kg_parity_gaps`)
across both graphs.

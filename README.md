# codebase-kg

A Claude Code plugin that **builds, maintains, and serves a per-repo code graph** — a structured,
source-derived map of any codebase so an agent can orient and find code *without re-deriving the map
every session*.

- **Fast at any size.** The graph is a committed SQLite file. Opening it is ~1 ms whether it holds
  175 nodes or 100,000; search runs through a persisted FTS5 index.
- **Correct by construction.** Dangling edges, duplicate ids, inconsistent parity and line-number
  anchors aren't reported after the fact — the store refuses to contain them.
- **Language-agnostic.** Node `kind` is free text, source reading is agent-driven, all per-repo
  specifics live in config. No language is hardcoded anywhere — not even in "which files are source",
  which is derived from the extensions the graph already anchors on.
- **Symbol-anchored.** Nodes point at `path#Symbol`, never line numbers — so the map doesn't rot on
  every edit.
- **Cross-codebase parity.** A node can link directly to its counterpart in *another* repo's graph,
  carrying a parity status + one-line divergence — so "find all feature gaps between the iOS app and
  its Android port" is a query across two graphs, not a hand-maintained file.
- **Advisory, with one gate.** Drift and validation surface as advice; they never gate a build or a
  tool, and never a commit. The push hook blocks on exactly one thing: any mapped file that has
  drifted, including files the push touches. Zero in a repo that is kept current, and releasable
  with an acknowledgement that names the count.

The graph is **descriptive** (a mirror of current code), **source-derived** (never ticket- or
history-derived), and **point-don't-copy** (references symbols, never pastes code). It is a tool
that reflects code state to reduce search cost. Nothing more.

## How it works

Each repo gets a committed `knowledge/code_graph.db`. A node is a row:

| field | example |
|---|---|
| `id` | `feed_ranker` |
| `kind` | `Domain (pure)` |
| `description` | Ranks the feed by freshness, breaking flag and normalized source/category weight. |
| `anchors` | `domain/FeedRanker.kt#FeedRanker`, `domain/FeedRanker.kt#ExploreExploitBalancer` |
| `edges` | `feed_view_model`, `reading_event_entity` |

For a paired codebase the node also carries `parity` / `counterpart` / `divergence`, linking it to
the peer repo's graph. See **[`SCHEMA.md`](SCHEMA.md)** for the full spec and
**[`docs/examples/EXAMPLE_GRAPH.json`](docs/examples/EXAMPLE_GRAPH.json)** for a worked iOS↔Android
slice.

**`description` is one short line — what it is and what it does.** It carries no ticket ids, no
dates and no account of what changed; that content duplicates git and is the whole reason the
previous Markdown format needed constant tending. The builder rejects all three.

### Authoring

Two ways in, and the size of the change picks between them.

**Targeted edits — the write tools.** One node's description, an anchor that moved, a link into
another graph. Each call is atomic, is validated against the whole graph before it lands, and
reports every field it changed, before and after.

**Bulk work — the round trip.** A parity sweep, a restructuring, anything where reading the diff
before applying it is the point. The artifact is a database, so it is authored through JSON. The
CLIs run through a stdlib runner beside the MCP launcher, from any repo; `<plugin>` is the plugin
folder, which skills name as `${CLAUDE_PLUGIN_ROOT}`:

```sh
uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" export -o .kg-export.json      # existing graph → JSON
#   … edit …
uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                   # a snapshot, not a source
```

The round trip is lossless — building an unedited export is byte-identical — so anything in the git
diff is a change you actually made. The builder validates before writing and, if a node breaks a
rule, names it and writes nothing.

`/codebase-kg:build` and `/codebase-kg:refresh` drive this for you.

## The tools

Sixteen MCP tools over the graph — ten queries and six targeted writes.

| query | answers |
|---|---|
| `kg_search` | "where is bookmark persistence?" — ranked FTS5 search over ids, kinds, descriptions and anchors |
| `kg_node` | the full record for one id, plus who points at it |
| `kg_find_by_path` | "I have this file open — what is it, and what connects to it?" |
| `kg_neighborhood` | everything within N hops, following edges either way |
| `kg_find_by_kind` | every `ViewModel` / `Service` / `@Entity` |
| `kg_find_by_link` | which code node(s) point at a node in another committed graph |
| `kg_find_by_reference` | "this SDK page moved — which code relies on it?" — nodes by the documentation they cite |
| `kg_parity_gaps` | the cross-codebase gap report, as a query |
| `kg_stats` | cold-start orientation: counts, kinds, sections, isolated nodes, and the repo-wide staleness total — how much of this map is out of date, and which nodes |
| `kg_validate` | advisory drift check against real source: anchors that no longer resolve, declared coverage gaps, and files edited since the graph was built |

| write | does |
|---|---|
| `kg_upsert_node(nodes)` | creates or updates node(s); only the fields you supply change |
| `kg_delete_node(ids, dry_run=true, cascade_inbound=false)` | previews the blast radius, then deletes |
| `kg_add_link(node_id, target, kind)` | points a node at a node in another committed graph |
| `kg_remove_link(node_id, target)` | drops one such pointer |
| `kg_add_reference(node_id, url, kind, title, path, symbol)` | records where a fact the node depends on is documented |
| `kg_remove_reference(node_id, url, path, symbol)` | drops a node's reference(s) to a url |

A write runs against a private copy of the file, inside one transaction, and the copy replaces the
original only after `kg_validate` confirms it introduced no new finding. So a rejected edit leaves
the committed graph **byte-identical** — not rolled back, never opened for writing.

## The server

One `codebase-kg --serve` process per machine and build serves every session. Claude Code reaches it
over Streamable HTTP at `http://127.0.0.1:47821/mcp`; other MCP clients (Codex, any stdio client)
launch `mcp/launch/kg-shim`, which relays stdio to the same process over a loopback socket.

- **Starting it.** A `SessionStart` hook starts the server when nothing answers `GET /health` on the
  port, and waits up to 3 s. Claude Code retries the connection for about 7 s after a session starts,
  then gives up for that session. The first start after an update builds the server's venv and can
  take longer; the next session then connects.
- **Which repo.** The server asks Claude Code for the session's roots on the first tool call and
  resolves the graph from the first root, as a shim resolves it from its cwd. `CODEBASE_KG_PATH`
  still names a graph explicitly.
- **Port.** The plugin setting `server_port` (default 47821) moves it. When another program holds
  the port, the hook prints one line naming the setting. A newer codebase-kg build takes the port
  from an older one; an older build leaves a newer one running.
- **Security.** The server binds `127.0.0.1` only, refuses any `Host` but its own loopback address
  and any foreign `Origin`, and needs a bearer token that only your user account can read. The
  `headersHelper` (`mcp/launch/kg_headers.py`) sends that token only after the server on the port
  proves, through its state file, to be yours.
- **Lifetime.** A server that holds the HTTP port exits after 8 hours with no request; a shim-only
  server exits after 10 minutes with no connection.
- **Workers.** The server runs each read tool on a worker process: a plain Python interpreter of
  about 24 MB that imports no MCP code. A call that finds no idle worker starts one, up to the
  `max_workers` setting (default 4); later calls wait for a free one. A worker exits after 60 s with
  no call, so an idle server holds only its own process. A worker that crashes, or takes longer than
  60 s (`CODEBASE_KG_CALL_TIMEOUT`), fails that call alone, and the next call starts a fresh one.
  The writes run in the server process. `max_workers` 0 runs every call there, as before 0.12.0.
- **Changing `max_workers`.** The server reads the setting when it starts and keeps it while it
  runs, so a change applies to the next server. To apply it at once, end the server process (its pid
  is in `GET /health`, with the `max_workers` it runs); the next session starts a new one.
  `CODEBASE_KG_MAX_WORKERS` overrides the setting.

## Migrating from `KNOWLEDGE_GRAPH.md`

Once per repo:

```sh
uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" migrate knowledge/KNOWLEDGE_GRAPH.md
```

It **converts, not regenerates** — ids, kinds, anchors, edges and parity survive verbatim. It
scrubs ticket refs, dates and change narrative out of the summaries, drops any dangling edges, and
reports everything it changed. The markdown file is left untouched; delete it once you are happy.

Add `*.db binary diff=codegraph` and `code_graph.db binary diff=codegraph` to the repo's
`.gitattributes` and commit the result, then run `/codebase-kg:setup` — it wires the staleness checks
and the textconv driver that makes the graph show up as a readable diff in review.

### Already on a `code_graph.db`?

A graph built before schema v3 has no declared coverage and no source baselines, so it cannot report
a file type it never mapped or a description whose code moved underneath it. Upgrade in place:

```sh
uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" upgrade --covers 'app/src/**/*.kt' --covers '**/*.gradle.kts'
```

Every node, anchor and edge is preserved verbatim; the upgrade only adds what v3 can answer.

## Layout

```
codebase-kg/
├── .claude-plugin/
│   ├── plugin.json           # plugin manifest (name: codebase-kg)
│   └── marketplace.json      # thin marketplace so it's installable standalone
├── SCHEMA.md                 # ← the spec: node shape, anchors, parity, the description contract
├── README.md
├── docs/
│   ├── DESIGN.md             # locked design decisions, genericity rules, principles
│   ├── RELEASING.md          # release checklist — the tag is load-bearing, not bookkeeping
│   └── examples/EXAMPLE_GRAPH.json
├── templates/
│   ├── code_graph.template.json
│   └── codebase-kg.local.md.example
├── .mcp.json                 # registers the codebase-kg HTTP server, with mcp/launch/kg_headers.py as its headersHelper
├── skills/                   # query / build / refresh / audit / link / validate, and setup (user-invoked only)
├── mcp/                      # the query server (one shared process per machine) + build/export/migrate CLIs
├── hooks/                    # Claude Code hooks: the search gate, the push gate, the post-edit nudge, the unwired-clone notice, the server start
└── git-hooks/                # advisory pre-commit + pre-push staleness checks and install.sh, vendored into any repo (stdlib-only)
```

## Making the graph get used

A map nobody opens is worth nothing. Left alone, an agent reaches for `Grep` and re-derives the map
it already has — slower, and blind to the components a search string does not appear in.

The **search gate** (`hooks/kg_search_gate.py`, a `PreToolUse` hook) fixes that. In any repo that has
a `knowledge/code_graph.db`, a `Grep`, `Glob`, or shell `grep`/`rg`/`find -name` aimed at mapped code
is denied with the instruction to query the graph first. A codebase-kg query clears the next
`gate_credit` searches (default 3); when that credit runs out, the gate denies again. A search scoped
to a file the graph anchors is never gated. Inside a subagent, the gate adds the instruction to the
subagent's context instead of denying.

No search is ever permanently blocked: if the graph does not cover what you need, run the same
search again and it goes through, however you reword the command.

It ships with the plugin, so there is nothing to install — it activates in every repo that has a
graph, and stays silent in every repo that does not. `SKIP_KG=1` silences it for a shell;
`search_gate: warn` in `.claude/codebase-kg.local.md` downgrades it to a message, and
`search_gate: off` disables it.

The `query` skill (`/codebase-kg:query`) is the workflow it hands off to: `kg_search` to locate,
`kg_neighborhood` to expand, then read the anchored files.

The division of labor it enforces: **the graph is authoritative for where code lives; the source is
authoritative for what it does now.** The graph is a committed snapshot, so orient with it, then read
the anchored files to confirm behavior.

## Keeping the graph in sync with commits

Two advisory layers, both pointing at the same fix (`/codebase-kg:refresh`):

- **In-session nudge** (`hooks/`): while Claude edits source, it says so the first time you touch a
  file no node covers, and periodically once enough mapped files have changed.
- **Push gate for agents** (`hooks/kg_push_gate.py`, a `PreToolUse` hook on Bash): when an agent runs
  `git push` (also `git -C <dir> push`, chained, or env-prefixed) while any mapped file is stale
  against `HEAD`, the hook denies the call. The reason lists the stale files and tells the agent to run
  `/codebase-kg:refresh`, commit the graph and push again. `SKIP_KG=1` and a matching
  `KG_STALE_ACK=<n>` pass it. `--no-verify` does not, because it skips git hooks and not this one. The
  hook fails open on any error. It uses the pre-push hook's comparison, so both agree on what is stale.
- **Commit/push checks** (`git-hooks/`): installed per-repo via `/codebase-kg:setup`. They compare
  the change set against the graph and report three things: source no node covers, deleted files the
  graph still anchors on, and mapped files whose contents no longer match the digest recorded when
  the graph was built. That third one is the one that catches ordinary work — a change set is mostly
  modifications, and a check that reads only additions and deletions is silent through most of the
  drift. Both also report the **repo-wide** total, which no change set can see: a file that drifts
  and is never re-derived is named once and then never again. Commit-time is one line and never
  blocks; push-time blocks on every stale mapped file (`KG_STALE_ACK=<n>`, `SKIP_KG=1` or
  `--no-verify` to get past it). Before it blocks, the push hook runs
  `claude -p "/codebase-kg:refresh"` and commits the refreshed graph, then asks you to push again.
  That costs one headless model run per stale push; `KG_AUTO_REFRESH=0` turns it off. They're
  stdlib-only and vendored into the repo, so they run for every clone and CI.

### One command per clone

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`, and **git never clones
`.git/config`** — deliberately, so that cloning a repo cannot make it execute code. So a repo can
commit the checkers, the wrappers and the `.gitattributes` line and still hand every fresh checkout
inert hooks and `Binary files differ`, with no error anywhere to say so.

`/codebase-kg:setup` therefore vendors `git-hooks/install.sh` into the repo alongside the checkers.
Everyone who clones runs it once:

```sh
sh .githooks/install.sh
```

Plain git, POSIX sh and `uv` — no Claude Code, no plugin install. Idempotent. It sets
`core.hooksPath`, fixes the exec bits, registers the textconv driver against a **tag-pinned** remote
(never a local plugin-cache path, which resolves on one machine and breaks on the next update), and
then probes the driver against the real graph rather than trusting the value it just wrote.

The plugin's `SessionStart` hook prints one line in a clone that has not run it. It only prints — a
plugin the user installed may suggest, but wiring `core.hooksPath` for them would route around the
protection git is providing.

Neither checks a date. An earlier version blocked pushes when the graph's `refreshed:` header wasn't
today; that measured whether someone edited the file, not whether the nodes matched the code — and
it went wrong in practice. See [`docs/DESIGN.md`](docs/DESIGN.md).

Because refresh reads the peer graph, two linked repos stay **eventually consistent**: each side
reconciles parity when *it* commits, so you only ever manage one repo's commit at a time.

## Design lineage

Mirrors the structure of the author's `a11y` plugin (skill + MCP + advisory-hook) and its `a11y-kg`
MCP query server, generalized past accessibility to *any* codebase.

## License

Proprietary. All rights reserved — see [`LICENSE`](LICENSE) and [`EULA.md`](EULA.md).
No license to use is granted without a written agreement.

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
- **Advisory, never blocking.** Drift and validation surface as advice; they never gate a commit,
  build, or tool.

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
before applying it is the point. The artifact is a database, so it is authored through JSON:

```sh
python -m codebase_kg.export -o .kg-export.json      # existing graph → JSON
#   … edit …
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                   # a snapshot, not a source
```

The round trip is lossless — building an unedited export is byte-identical — so anything in the git
diff is a change you actually made. The builder validates before writing and, if a node breaks a
rule, names it and writes nothing.

`/codebase-kg:build` and `/codebase-kg:refresh` drive this for you.

## The tools

Thirteen MCP tools over the graph — nine queries and four targeted writes.

| query | answers |
|---|---|
| `kg_search` | "where is bookmark persistence?" — ranked FTS5 search over ids, kinds, descriptions and anchors |
| `kg_node` | the full record for one id, plus who points at it |
| `kg_find_by_path` | "I have this file open — what is it, and what connects to it?" |
| `kg_neighborhood` | everything within N hops, following edges either way |
| `kg_find_by_kind` | every `ViewModel` / `Service` / `@Entity` |
| `kg_find_by_link` | which code node(s) point at a node in another committed graph |
| `kg_parity_gaps` | the cross-codebase gap report, as a query |
| `kg_stats` | cold-start orientation: counts, kinds, sections, isolated nodes |
| `kg_validate` | advisory drift check against real source: anchors that no longer resolve, declared coverage gaps, and files edited since the graph was built |

| write | does |
|---|---|
| `kg_upsert_node(nodes)` | creates or updates node(s); only the fields you supply change |
| `kg_delete_node(ids, dry_run=true, cascade_inbound=false)` | previews the blast radius, then deletes |
| `kg_add_link(node_id, target, kind)` | points a node at a node in another committed graph |
| `kg_remove_link(node_id, target)` | drops one such pointer |

A write runs against a private copy of the file, inside one transaction, and the copy replaces the
original only after `kg_validate` confirms it introduced no new finding. So a rejected edit leaves
the committed graph **byte-identical** — not rolled back, never opened for writing.

## Migrating from `KNOWLEDGE_GRAPH.md`

Once per repo:

```sh
python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md
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
python -m codebase_kg.upgrade --covers 'app/src/**/*.kt' --covers '**/*.gradle.kts'
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
│   └── examples/EXAMPLE_GRAPH.json
├── templates/
│   ├── code_graph.template.json
│   └── codebase-kg.local.md.example
├── .mcp.json                 # registers the codebase-kg MCP server (uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp)
├── commands/                 # /codebase-kg:query|build|refresh|audit|link|validate → skills; setup is inline
├── skills/                   # kg-query / kg-build / kg-refresh / kg-audit / kg-link / kg-validate
├── mcp/                      # the query server + build/export/migrate CLIs (uvx-run Python)
├── hooks/                    # Claude Code hooks: the search gate + the post-edit nudge
└── git-hooks/                # advisory pre-commit + pre-push staleness checks, vendored into any repo (stdlib-only)
```

## Making the graph get used

A map nobody opens is worth nothing. Left alone, an agent reaches for `Grep` and re-derives the map
it already has — slower, and blind to the components a search string does not appear in.

The **search gate** (`hooks/kg_search_gate.py`, a `PreToolUse` hook) fixes that. In any repo that has
a `knowledge/code_graph.db`, the first `Grep`, `Glob`, or shell `grep`/`rg`/`find -name` of a session
is denied once, with the instruction to query the graph first. Then it **stands down for the rest of
that session** — whether or not the agent complied. Querying any codebase-kg MCP tool stands it down
too, so an agent that already started at the graph never sees it.

One interruption per session. It cannot loop, and no search is ever permanently blocked: if the
graph does not cover what you need, run the search again and it goes through.

It ships with the plugin, so there is nothing to install — it activates in every repo that has a
graph, and stays silent in every repo that does not. `SKIP_KG=1` silences it for a shell;
`search_gate: warn` in `.claude/codebase-kg.local.md` downgrades it to a message, and
`search_gate: off` disables it.

The `kg-query` skill (`/codebase-kg:query`) is the workflow it hands off to: `kg_search` to locate,
`kg_neighborhood` to expand, then read the anchored files.

The division of labor it enforces: **the graph is authoritative for where code lives; the source is
authoritative for what it does now.** The graph is a committed snapshot, so orient with it, then read
the anchored files to confirm behavior.

## Keeping the graph in sync with commits

Two advisory layers, both pointing at the same fix (`/codebase-kg:refresh`):

- **In-session nudge** (`hooks/`): while Claude edits source, it says so the first time you touch a
  file no node covers, and periodically once enough mapped files have changed.
- **Commit/push checks** (`git-hooks/`): installed per-repo via `/codebase-kg:setup`. They compare
  the change set against the graph and report three things: source no node covers, deleted files the
  graph still anchors on, and mapped files whose contents no longer match the digest recorded when
  the graph was built. That third one is the one that catches ordinary work — a change set is mostly
  modifications, and a check that reads only additions and deletions is silent through most of the
  drift. They **never block** — there is no `--no-verify` to remember. They're stdlib-only and
  vendored into the repo, so they run for every clone and CI.

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

# codebase-kg

A Claude Code plugin that **builds, maintains, and serves a per-repo knowledge graph** — a
structured, source-derived map of any codebase so an agent can orient and find code *without
re-deriving the map every session*.

- **Language-agnostic.** Node `kind` is free text, source reading is agent-driven, all
  per-repo specifics live in config. No language is hardcoded anywhere.
- **Symbol-anchored.** Nodes point at `path#Symbol`, never line numbers — so the map doesn't
  rot on every edit.
- **Cross-codebase parity.** A node can link directly to its counterpart in *another* repo's
  KG, carrying a parity status + one-line divergence — so "find all feature gaps between the
  iOS app and its Android port" is a query across two graphs, not a hand-maintained file.
- **Advisory, never blocking.** Freshness/drift/validation surface as advice; they never gate
  a commit, build, or tool.

The KG is **descriptive** (a mirror of current code), **source-derived** (never ticket-derived),
and **point-don't-copy** (references symbols, never pastes code). It is a tool that reflects code
state to reduce search cost. Nothing more.

## Status

🚧 **Early scaffold.** This session set up the repo, the schema, and the design docs. The MCP
server and skills are not built yet — see the build phases below.

| Phase | What | State |
|---|---|---|
| 1 | Schema + format; one example KG | ✅ `SCHEMA.md`, `docs/examples/EXAMPLE_KG.md` |
| 2 | MCP server (parse markdown KG → queryable graph; the §5 tools) | ⬜ `mcp/` (see `docs/MCP_SURFACE.md`) |
| 3 | Skills — `kg-build` / `kg-refresh` / `kg-audit` / `kg-link` / `kg-validate` | ⬜ `skills/` |
| 4 | Advisory post-edit freshness hooks | ⬜ `hooks/` |
| 5 | Cross-codebase parity (`counterpart` resolution + `kg_parity_gaps`) | ⬜ |
| 6 | Dogfood on the Acme iOS↔Android pair | ⬜ |

## How it works

Each repo gets a `KNOWLEDGE_GRAPH.md` (markdown is the source of truth — diffable, greppable;
the MCP parses it into a graph). A node is a small key/value table:

```
| id      | feed_ranker |
| kind    | Domain (pure) |
| anchors | `domain/FeedRanker.kt#FeedRanker`, `domain/FeedRanker.kt#ExploreExploitBalancer` |
| summary | Ranks the feed by freshness + breaking + per-source/category weight. Wired in acme_view_model.fetch(). |
| edges   | reading_event_entity, acme_view_model |
```

For a paired codebase, the node also carries `parity` / `counterpart` / `divergence` linking it
to the peer repo's KG. See **[`SCHEMA.md`](SCHEMA.md)** for the full spec and
**[`docs/examples/EXAMPLE_KG.md`](docs/examples/EXAMPLE_KG.md)** for a worked iOS↔Android slice.

## Layout

```
codebase-kg/
├── .claude-plugin/
│   ├── plugin.json           # plugin manifest (name: codebase-kg)
│   └── marketplace.json      # thin marketplace so it's installable standalone
├── SCHEMA.md                 # ← the spec: node shape, anchors, parity, update policy
├── README.md
├── docs/
│   ├── DESIGN.md             # locked design decisions, genericity rules, principles
│   ├── MCP_SURFACE.md        # the planned MCP tool surface (Phase 2 design)
│   ├── BUILD_PLAN.md         # the full 6-phase build plan (carried in)
│   └── examples/EXAMPLE_KG.md
├── templates/
│   ├── KNOWLEDGE_GRAPH.template.md
│   └── codebase-kg.local.md.example
├── skills/   (Phase 3)       # kg-build / kg-refresh / kg-audit / kg-link / kg-validate
├── mcp/      (Phase 2)       # the markdown-KG query server (uvx-run Python, like a11y-kg)
└── hooks/    (Phase 4)       # advisory post-edit freshness nudge
```

## Design lineage

Mirrors the structure of the author's `a11y` plugin (skill + MCP + advisory-hook) and its
`a11y-kg` MCP query server, generalized past accessibility to *any* codebase. The node style is
generalized from the Acme Android `KNOWLEDGE_GRAPH.md` (a real, audited 700-line KG): `type→kind`,
`files→anchors` (now symbol-based), `details→summary`, `deps→edges`, plus the new
`parity`/`counterpart`/`divergence` fields.

## License

MIT — see [`LICENSE`](LICENSE).

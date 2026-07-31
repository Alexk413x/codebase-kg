# Codebase Code-Graph — Schema

> The canonical spec for a `code_graph.db`. Generic across languages and repos.
> Skills generate and refresh graphs to this shape; the MCP server queries it.
> If you change the schema, change it here first — and then in
> `mcp/src/codebase_kg/schema.py`, which is the executable copy of §4–§6.

## 0. What a code graph is (and is not)

A code graph is a **source-accurate index of one codebase**. Nodes are the codebase's meaningful
units (a screen, a model, a service, a module, an actor); edges are the relationships between them.
An agent reads the graph to **orient and find code without re-deriving the map every session**.

The graph is a **tool that reflects current code state to reduce search cost. Nothing more.**

- **Descriptive, not prescriptive.** It records what the code *is*. It does not assign work,
  prioritize, or track tickets.
- **Source-derived, not history-derived.** Every claim traces to code as it stands. Tickets, dates
  and "what changed" belong to git and the tracker; a node that carries them is carrying a copy that
  will go stale. §5.1 makes this a rule the store enforces.
- **Point, don't copy.** Reference symbols; never paste code into the graph. Copied code rots.
- **Advisory, never blocking.** Freshness, drift, and validation surface as advice. They never gate
  a commit, build, or tool.
- **Machine-first.** The artifact is a database, read by tools. It is not meant to be read raw or
  reviewed in a diff.

## 1. One graph per repo

One `code_graph.db` per codebase, **identified by where it lives** (its repo / directory), not by a
platform-prefixed filename. The iOS graph lives in the iOS repo; the Android graph lives in the
Android repo. A repo's graph is generic — nothing in the schema is platform-specific.

Filename: `code_graph.db`. **Location: a `knowledge/` subdirectory** (`knowledge/code_graph.db`).
This is the single convention: there is **no repo-root fallback**. The location is configurable per
clone via `graph_path` — see §8 and `templates/codebase-kg.local.md.example`.

**It is committed.** The artifact reflects the code's current state and travels with the code.

Add this to the repo's `.gitattributes` so git treats it as the binary it is:

```gitattributes
knowledge/code_graph.db binary
```

Git's packfile delta compresses it across refreshes regardless — SQLite's page layout means
unchanged rows sit in unchanged pages — so committed history stays small.

## 2. Why a database

The predecessor format was a single Markdown file parsed on every load. Three things forced the
change, in order of importance:

| | Markdown | `code_graph.db` |
|---|---|---|
| **Load cost** | O(nodes) — parse the whole file every time | **O(1)** — ~1 ms at any size |
| **Integrity** | checked after the fact by `kg_validate` | **enforced at write time** by constraints |
| **Search** | linear scan over every node | persisted **FTS5** index, ~0.5 ms |
| **Update** | rewrite the whole file | change the rows that changed |

The integrity row is the one that matters most. A dangling edge is not a thing the graph reports —
it is a thing the file physically cannot contain (§6).

## 3. The `meta` table

Config and provenance, as key/value rows. The MCP server and the hooks read it.

| key | required | meaning |
|---|---|---|
| `schema_version` | ✅ | Set by the writer. A reader that speaks a different version refuses to open the file rather than guessing. |
| `codebase` | ✅ | Short name — `android`, `ios`, `web`, `backend`. Used in `<codebase>-only` parity flags. |
| `root` | ✅ | The code root this graph describes, **repo-relative** (e.g. `app/src/main`). Anchor paths are relative to it. |
| `generated` | ✅ | `YYYY-MM-DD` the artifact was last built. **Provenance, not a contract** — nothing gates on it (§7). |
| `counterpart` | ⬜ | Path to a paired repo's `code_graph.db`, for parity. Omit for a single-codebase repo. |
| `language` | ⬜ | Hint for symbol tooling only. Grep always works; ctags/LSP are opportunistic. |

`root` must be repo-relative: the graph is committed and shared, so an absolute `root` is per-clone
and won't match anywhere else.

## 4. The `node` table

One row per component.

| column | required | meaning |
|---|---|---|
| `id` | ✅ | Stable kebab/snake slug, **primary key**. The handle every edge and counterpart link uses. Name it for the *concept*, not the file (`saved_articles`, not `SavedArticleEntity_kt`) so renaming a file doesn't force an id change. |
| `kind` | ✅ | **Free-text** role: `Composable`, `ViewModel`, `actor`, `@Model`, `Service`, `module`, `Repository`, `enum`, `hook`, `route`, … Free-form *by design* — this is what keeps the schema language-agnostic. Do NOT constrain it to a fixed enum. |
| `description` | ✅ | One short line: what it is and what it does. **≤ 240 chars**, present tense, no history. See §5. |
| `section` | ⬜ | Free-text grouping (`APP ENTRY`, `SERVICES`, …). Display only, no semantics. |
| `parity` | ⬜ | `matched`, `divergent`, or `<codebase>-only`. Multi-codebase only. §9. |
| `counterpart` | ⬜ | `<peer-graph-path>#<node-id>`. §9. |
| `divergence` | ⬜ | One line naming *how* the two sides differ. Only when `parity = divergent`. |

## 5. The `description` contract

A description says **what the component is and what it does**. That is the part an agent cannot
cheaply recover by grepping, so it earns its place.

It must **not** carry:

- **ticket ids** (`ACME-431`) — the tracker has them
- **dates** — git has them
- **change narrative** ("now sends", "no longer pauses", "Live-verified: …", "replaced X") — a
  description states the present; the past is a commit message

This is not style advice. It is the finding that motivated the rewrite: in the live Android graph
the summaries were 62% of the payload, carried 171 ticket refs, and were the *sole* reason the
format needed a maintenance ceremony at all. Structure regenerates correctly; hand-written history
rots.

### 5.1 Enforced, not requested

`length(description) <= 240` is a CHECK constraint. The rest is enforced by the writer, which
refuses to build a graph containing a violation and names the offending node. Both surfaces run the
same rules (`mcp/src/codebase_kg/clean.py`), and `kg_validate` reports any that somehow got in.

### 5.2 Good and bad

```
✅  Foreground Media3 MediaSessionService owning the video feed's ExoPlayer;
    survives backgrounding with a media notification and lock-screen transport.

❌  Foreground Media3 MediaSessionService for the video feed (ACME-433) so video
    playback survives backgrounding. Mirrors audio_service (ACME-146).
    Live-verified 2026-06-19: screen-off keeps state=PLAYING.
```

Same component. The first is durable; the second needed re-verifying every sprint.

## 6. `anchor` and `edge`

### 6.1 Anchors — symbol pointers, never line numbers

One row per anchor: `(node_id, ord, path, symbol)`. `path` is relative to `meta.root`; `symbol` is a
grep-resolvable name, or NULL for a whole-file anchor (a manifest, a config file). `ord` preserves
the author's order.

Line numbers are the **#1 source of drift** — they rot on every edit above them. Symbols
(`Article`, `FeedRanker.rank`) are **stable**, **greppable**, and **universal**. A CHECK constraint
rejects a symbol that starts with a digit, so a line-number anchor cannot be written.

`path` is indexed, which is what makes `kg_find_by_path` — "which node owns this file?" — a lookup
rather than a scan.

### 6.2 Edges — intra-graph, and provably resolvable

One row per relationship: `(src, dst)`, both foreign keys into `node(id)`.

- `dst` is a foreign key, so **a dangling edge cannot be written**.
- `(src, dst)` is the primary key, so a duplicate edge cannot be written.
- `src <> dst` is a CHECK, so a self-edge cannot be written.
- `ON DELETE RESTRICT` on `dst`, so removing a node cannot silently strand what points at it.

Edges stay *within one graph*. Cross-graph links are `counterpart` (§9), not edges.

### 6.3 `node_fts` — the search index

An FTS5 table over each node's id, kind, description and anchors. CamelCase identifiers are indexed
in split form as well as whole, so `video playback` finds `VideoPlaybackService`.

The index is **persisted in the file**. It is built once at write time and costs nothing on open —
which is why it is affordable here and was not when the format was Markdown, where any index had to
be rebuilt on every load.

## 7. Keeping it current

There is no freshness contract, because a date cannot express one. The old format had a
`refreshed:` header and a push gate that compared it to today; the header could be — and was —
bumped without the nodes changing.

Staleness is answered by comparing the graph to the code:

- **`kg_validate`** — anchors whose file or symbol no longer exists, and source files under `root`
  that no node covers. Both are facts about the source tree.
- **the pre-push check** — the same two questions, scoped to the commits you are pushing. It reports
  and exits 0. It never blocks.
- **the post-edit hook** — says so the first time you edit a file no node anchors on.

The rule for an update is unchanged and still matters: **update all affected nodes** — add nodes for
new files/features, edit nodes whose code changed, remove nodes for deleted code. What is gone is
the ceremony around proving you did.

## 8. Per-repo config

Everything repo-specific is **config, not code** — and the project-level config is the committed
`meta` table itself (§3). It is shared with the team because the graph is committed:

- `codebase`, `root`, `counterpart`, `language` — read by the MCP server, the hooks and the
  pre-push check.

`graph_path` (where the file lives) defaults to `knowledge/code_graph.db`, with **no repo-root
fallback**. Because all of the above lives in the committed graph, **no separate config file is
needed**.

A `.claude/codebase-kg.local.md` is an **optional, per-developer override** — by the `.local`
convention it is gitignored, never committed. Use it only when one clone differs (a peer repo at a
non-standard path, a custom `graph_path`). The older key name `kg_path` is still read.

## 9. Cross-codebase parity

Two paired codebases keep **two independent graphs**, each a true mirror of *its own* source. Parity
is expressed by **direct node cross-linking**, not a third artifact:

- A node carries `counterpart` → the paired node in the other graph, plus a `parity` status and
  (when divergent) a one-line `divergence`.
- Detail stays on each side. No duplication.
- "Find all gaps" is a **query** (`kg_parity_gaps`), not a file.
- Links are **reciprocal**; validation flags a `counterpart` the other side doesn't link back to.

The three shapes — and these are the *only* three the store will accept:

| `parity` | Meaning | `counterpart` | `divergence` |
|---|---|---|---|
| `matched` | Same feature, equivalent implementation (allowing expected platform tooling: SwiftUI↔Compose, SwiftData↔Room, Combine↔Flow). | **required** | must be absent |
| `divergent` | Both have it, implemented differently enough to flag. | **required** | **required** |
| `<codebase>-only` | Present here, absent from the peer **in code** (a PRD mention does not count — verify against source). | must be absent | must be absent |

Every one of those rules is a CHECK constraint. A half-filled parity triple is not a validation
finding; it is a failed write.

## 10. Genericity rules (do not violate)

- **No language hardcoding** in the schema, skills, or MCP. `kind` is free text; source reading is
  agent-driven, so any language is handled. Even the "which files are source?" question is answered
  from the extensions the graph already anchors on, never from a built-in list.
- **Symbol discovery via Grep always**, ctags/LSP only when present — never assume a toolchain.
- **All per-repo specifics live in config** (§8), never in the generic components.

## 11. Migrating from `KNOWLEDGE_GRAPH.md`

Once per repo:

```sh
python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md
```

It converts rather than regenerates — ids, kinds, anchors, edges and parity survive verbatim — and
reports what it changed: descriptions scrubbed, dangling edges dropped, parity triples normalized.
The markdown file is left untouched; delete it once you are satisfied.

Per-node `updated` dates and the header's accumulated refresh log do not carry over. They were the
duplicated-from-git content this rewrite exists to remove.

# Codebase Knowledge-Graph — Schema

> The canonical spec for a `KNOWLEDGE_GRAPH.md`. Generic across languages and repos.
> Skills generate and refresh KGs to this shape; the MCP server parses this shape into a
> queryable graph. If you change the schema, change it here first.

## 0. What a KG is (and is not)

A knowledge graph is a **living, source-accurate index of one codebase**. Nodes are the
codebase's meaningful units (a screen, a model, a service, a module, an actor); edges are
the relationships between them. An agent reads the KG to **orient and find code without
re-deriving the map every session**.

The KG is a **tool that reflects current code state to reduce search cost. Nothing more.**

- **Descriptive, not prescriptive.** It records what the code *is*. It does not assign work,
  prioritize, or track tickets.
- **Source-derived, not ticket-derived.** Every claim traces to source. Tickets/PRDs are
  irrelevant to it. (A node *may* mention a ticket id as a breadcrumb, but the node exists
  because the *code* exists, and is verified against the *code*.)
- **Point, don't copy.** Reference symbols; never paste code into the KG. Copied code rots.
- **Advisory, never blocking.** Freshness, drift, and validation surface as advice. They
  never gate a commit, build, or tool.

## 1. One KG per repo

One `KNOWLEDGE_GRAPH.md` per codebase, **identified by where it lives** (its repo / directory),
not by a platform-prefixed filename. The iOS KG lives in the iOS repo; the Android KG lives in
the Android repo. A repo's KG is generic — nothing in the schema is platform-specific.

Filename: `KNOWLEDGE_GRAPH.md`. **Location: a `knowledge/` subdirectory**
(`knowledge/KNOWLEDGE_GRAPH.md`) — the home for the KG and any related committed reference docs.
This is the single convention: there is **no repo-root fallback**. The location is **configurable**
per clone via `kg_path` — see §7 and `templates/codebase-kg.local.md.example`.

## 2. Document structure

A KG file has this top-to-bottom shape. Sections in **bold** are required; the rest are
recommended once the codebase is large enough to warrant them.

```
# <Codebase> — Knowledge Graph        ← title
<header block>                         ← config + provenance (§3)
<update policy>                        ← the anti-drift contract (§6), copied verbatim

## NODES                               ← REQUIRED — the node tables (§4), grouped into sections
### <Section A>                        ← e.g. APP ENTRY, DOMAIN MODEL, SERVICES … (free-form grouping)
| id | … |  | kind | … |  | anchors | … |  | summary | … |  | edges | … |
### <Section B>
…

## EDGES                               ← relationship narrative (§5) — flows the node `edges` can't show linearly
### <flow name>
- A → B → C …

## FEATURE → CODE MAP                   ← optional search index: feature → primary anchors (a wide table)

## PARITY GAPS                          ← optional, multi-codebase only: a pointer, NOT a copy (§8)

## KEY DECISIONS                        ← optional: durable architectural constraints
## ARCHITECTURE SUMMARY                 ← optional: one or two paragraphs for cold-start orientation
```

Group nodes under `###` sections that fit *this* codebase (APP ENTRY, NAVIGATION, DOMAIN,
VIEW MODELS, SERVICES, MODULES, …). The grouping is for human scanning only — it carries no
semantics. **Do not** hardcode language-specific section names into tooling; they are free text.

## 3. The header block

Immediately under the title. Carries config (the MCP and skills read it) and provenance.

```
codebase:    <name>                 # e.g. "android", "ios", "web", "backend"
root:        <path to the code root, repo-relative>
counterpart: <path to a paired repo's KNOWLEDGE_GRAPH.md>   # optional; only for parity
language:    <hint, optional>       # e.g. "kotlin", "swift", "typescript" — for symbol tooling only
refreshed:   <YYYY-MM-DD>           # last comprehensive refresh (see §6)
```

`root` must be **repo-relative** (e.g. `app/src/main`): the KG file is committed and shared,
and the pre-push gate / advisory hook compare it against repo-relative paths — an absolute
`root` is per-clone and won't match.

Then one short paragraph: born date, last refresh action, and the one-line "what this doc is."
Then the **update policy** (§6), copied verbatim, so every reader sees the anti-drift contract.

## 4. The node

A node is a **key/value markdown table** — one node = a small block of `| key | value |` rows.
(This vertical form is used instead of one-row-per-node because `summary` is long. Short
indexes like FEATURE → CODE MAP may use a wide one-row-per-entry table.)

```
| id          | stable-slug                                   |
| kind        | free-text role                                |
| anchors     | `path#Symbol`, `path#OtherSymbol`             |
| summary     | what it is/does — THIS codebase only           |
| edges       | other-node-id, other-node-id                  |
| updated     | YYYY-MM-DD                                     |   ← optional; date this node was last verified vs source
| parity      | matched | divergent | <codebase>-only         |   ← optional, multi-codebase only
| counterpart | ../other-repo/KNOWLEDGE_GRAPH.md#other-node-id |   ← optional; omit if <codebase>-only
| divergence  | one short line                                |   ← optional; only when parity = divergent
```

### Field reference

| Field | Required | Meaning |
|---|---|---|
| `id` | ✅ | Stable kebab/snake slug, unique within the KG. The handle every edge and counterpart link uses. Name it for the *concept*, not the file (`saved_articles`, not `SavedArticleEntity_kt`) so a rename of the file doesn't force an id change. |
| `kind` | ✅ | **Free-text** role: `Composable`, `ViewModel`, `actor`, `@Model`, `Service`, `module`, `Repository`, `enum`, `hook`, `route`, … Free-form *by design* — this is what keeps the schema language-agnostic. Do NOT constrain it to a fixed enum. |
| `anchors` | ✅ | **Symbol-based pointers**: `path#Symbol`. Comma-separated. The `path` is repo-relative; `Symbol` is a grep-resolvable name (class/func/type/const). **Never line numbers** (see §4.1). A node with no symbol (e.g. a manifest, a config file) may anchor on the path alone. |
| `summary` | ✅ | What it is/does, **in this codebase only**. Pointer-dense, no copied code. Mention the symbols it touches, the migration it added, the wiring it participates in. This is the payload an agent reads instead of opening every file. |
| `edges` | ✅ | Intra-KG relationships: a list of other node `id`s this node depends on / relates to. Edges stay *within one KG*. (Cross-KG links are `counterpart`, not `edges`.) |
| `updated` | ⬜ | `YYYY-MM-DD` the node was last **added or verified against source**. Finer-grained than the header `refreshed` (which is the *whole-KG* refresh date): after a partial `kg-refresh`, only the touched nodes get a new `updated`, so a node whose `updated` lags `refreshed` is a candidate **stale** node. `kg-build` stamps every node with the build date; `kg-refresh` bumps it only on nodes it changes. Surfaced by `kg_stats` (`updated.stale_vs_refreshed`) and on every `kg_node`. |
| `parity` | ⬜ | Only in multi-codebase setups. One of `matched`, `divergent`, or `<codebase>-only` (e.g. `android-only`). The queryable gap flag. |
| `counterpart` | ⬜ | A direct link to the paired node in the other repo's KG: `<path-to-other-KG>#<node-id>`. Omit when `parity` is `<codebase>-only`. Reciprocal by convention — the other side links back (validated, see §8). |
| `divergence` | ⬜ | One short line naming *how* the two sides differ. Present only when `parity = divergent`. Detail stays in each side's `summary`; this is the headline. |

### 4.1 Why symbol anchors, never line numbers

Line numbers are the **#1 source of KG drift** — they rot on every edit above them, so a KG
pinned to lines is stale the moment anyone touches a file. Symbols (`Article`, `FeedRanker.rank`,
`SavedArticleEntity`) are **stable** (survive edits elsewhere in the file), **greppable** (any
agent with `Grep` resolves `path#Symbol` to a current line in one call), and **universal**
(every language has named symbols). Anchor on the symbol; let the reader resolve the line at
read time.

Format: `relative/path/to/file.ext#SymbolName`. For a method, either `File.ext#method` or
`File.ext#Type.method` — whatever greps uniquely. For a whole file with no single symbol, the
bare path is acceptable.

### 4.2 Worked example (single-codebase)

```
| id      | feed_ranker |
| kind    | Domain (pure) |
| anchors | `domain/FeedRanker.kt#FeedRanker`, `domain/FeedRanker.kt#ExploreExploitBalancer` |
| summary | Ranks the main feed by a weighted sum of freshness, breaking, source and category scores (stable sort; cold-start = identity). `ExploreExploitBalancer` (ε-greedy, injectable `Random`) floats a fresh lower-half article to position 1 ~10% of the time. Wired in `feed_view_model.fetch()`. |
| edges   | reading_event_entity, feed_view_model |
```

## 5. The EDGES section

The node `edges` field captures *local* dependencies, but it can't show a multi-hop flow
linearly. The `## EDGES` section narrates the flows the node table can't: navigation flow,
data flow, notification flow, DI/inversion, cross-cutting concerns. Each is a short
`A → B → C` chain referencing node ids (or their display names). This section is prose; the
node `edges` are the machine-readable truth.

## 6. Update policy (the anti-drift contract — copy this into every KG header)

> **Every KG update must fully reflect the change in the node tables, not just the header.**
> For each change: **add** nodes for new files/features, **edit** the nodes for changed
> files (anchors, summary, edges, parity), **remove/rename** nodes for deleted/renamed files,
> and refresh the affected `## EDGES` flows. **A header-or-date-only edit is forbidden** — that
> habit is exactly what causes drift (stale versions, dead symbol names, undocumented features).
> When you add a top-level component (screen, view-model, service, module, route, …), add a
> node and an edge to its dependencies. Bump `refreshed:` to today. A freshness check can verify
> the file was touched and the date matches; it **cannot** verify node completeness — that is
> on the author.

This contract is the reason the plugin exists. Lazy "bump the header" refreshes are the failure
mode it forbids.

## 7. Per-repo config

Everything platform- or repo-specific is **config, not code** — and the **project-level config is
the committed KG header itself (§3)**. It is shared with the team because the KG file is committed:

- `codebase` — short name (`android`, `ios`, `web`).
- `root` — the code root the KG describes (read by the MCP server and the pre-push gate).
- `counterpart` — the paired repo's KG path (omit for single-codebase repos).
- `language` — optional hint for symbol tooling (Grep always works; ctags/LSP are opportunistic).

`kg_path` (where the KG file lives) defaults to `knowledge/KNOWLEDGE_GRAPH.md` — the one convention,
with **no repo-root fallback**. Because all of the above is in the committed header, **no separate
config file is needed**; the tools read the header.

A `.claude/codebase-kg.local.md` is an **optional, per-developer override** — by the `.local`
convention it is **gitignored, never committed**. Use it only when one clone differs (e.g. a peer
repo checked out at a non-standard path, or a custom `kg_path`); it overrides the header for that
clone. The shared truth stays in the committed KG header. See `templates/codebase-kg.local.md.example`.

## 8. Cross-codebase parity

Two paired codebases (an app and its port; web and mobile) keep **two independent KGs**, each a
true mirror of *its own* source. Parity is expressed by **direct node cross-linking**, not a
third "parity" artifact:

- A node carries `counterpart:` → the paired node in the other KG, plus a `parity:` status and
  (when divergent) a one-line `divergence:`.
- Detail stays on each side (`summary` describes that side's implementation). No duplication.
- "Find all gaps" is a **query**, not a file: scan `parity` flags across both KGs
  (`kg_parity_gaps` once the MCP exists; `grep -n 'parity' KNOWLEDGE_GRAPH.md` by hand).
- Links are **reciprocal** by convention; validation flags a `counterpart` that the other side
  doesn't link back to, and a `counterpart` whose target id doesn't exist.

The three parity shapes:

| `parity` | Meaning | `counterpart` | `divergence` |
|---|---|---|---|
| `matched` | Same feature, equivalent implementation (allowing expected platform tooling: SwiftUI↔Compose, SwiftData↔Room, Combine↔Flow). | links to the peer | omit |
| `divergent` | Both have it, but implemented differently enough to flag (data shape, logic, placement, wiring). | links to the peer | one line naming the difference |
| `<codebase>-only` | Present in this codebase, absent from the peer in **code** (a PRD mention does not count — verify against source). | omit | omit |

An optional `## PARITY GAPS` section may point to "run `kg_parity_gaps`" or list the current
`divergent` / `*-only` ids as a convenience — but it is a **pointer/index, never a copy** of the
node detail. The nodes remain the source of truth.

## 9. Genericity rules (do not violate)

- **No language hardcoding** in the schema, skills, or MCP. `kind` is free text; source reading
  is agent-driven, so any language is handled.
- **Symbol discovery via Grep always**, ctags/LSP only when present — never assume a toolchain.
- **All per-repo specifics live in config** (§7), never in the generic components.

## 10. Format choice

Markdown is the **source of truth**: human-readable, diffable, greppable. The MCP server parses
these tables into a queryable graph at load time. (A machine-first `.json`/`.yaml` with a
generated `.md` view was considered and rejected as heavier; revisit only if parsing the
markdown proves too slow at scale.)

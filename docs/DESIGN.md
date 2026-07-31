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

## Resolved open questions

| Question | Decision | Note |
|---|---|---|
| **Storage format** | Committed SQLite (`code_graph.db`) | Constant-time open, write-time integrity, persisted FTS5. Supersedes "Markdown-as-source". |
| **Human readability** | Explicitly a non-goal | The artifact is AI-consumed. It is not read raw and not reviewed in diffs. |
| **Plugin name** | `codebase-kg` | Unchanged **on purpose** — see "The rename we didn't do". Commands `/codebase-kg:build\|refresh\|audit\|link\|validate\|install-hooks`; MCP server `codebase-kg`. |
| **MCP tool names** | `kg_*` | Unchanged, same reason. |
| **Repo layout** | Standalone plugin at root | `plugin.json` at root + a thin `marketplace.json` so it installs. |
| **Graph location** | Per-repo `knowledge/code_graph.db` | Always `knowledge/` (no repo-root fallback); override per clone via `graph_path` in `.local.md`. |
| **Refresh engine** | Agent-driven first | The agent reads source and emits nodes — keeps it language-agnostic. Add static parsers later for speed; never as the only path. |
| **Search index** | FTS5, persisted in the file | An in-memory index was deferred because building it landed on the load path. Persisting it removes that objection entirely: built once at write time, free on open. |
| **Counterpart direction** | Reciprocal | Both sides link; `kg-validate` flags one-directional or dangling links. |
| **Freshness gate** | Content check, advisory | Replaced the date-based push block. See "The gate that measured the wrong thing". |

## Why a database

Three properties, in the order they mattered:

**Integrity you cannot opt out of.** Foreign keys make a dangling edge unwritable. The primary key
makes a duplicate id unwritable. CHECK constraints encode the three legal parity shapes, so a
half-filled parity triple is a failed write rather than a validation finding. A transactional build
means a regeneration lands whole or not at all — there is no truncated-file state. This moved most
of `kg_validate`'s old job from "report afterwards" to "cannot happen", and what remains in the
report is only what a file genuinely cannot know about itself: whether its anchors still point at
real code.

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

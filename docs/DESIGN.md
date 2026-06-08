# Design — locked decisions, resolved questions, principles

The durable "why" behind the plugin. `SCHEMA.md` is the *what*; this is the rationale and the
settled calls. The full multi-phase plan lives in `BUILD_PLAN.md`.

## Locked design decisions

Carried verbatim from the build plan §2 — change these only with a deliberate reason.

1. **Per-repo KG.** One `KNOWLEDGE_GRAPH.md` per codebase, identified by where it lives, not a
   platform-prefixed filename. The iOS KG lives in the iOS repo; the Android KG in the Android
   repo. Generic across repos.
2. **Generic node schema** (language-agnostic): `id / kind / anchors / summary / edges / parity /
   counterpart / divergence`. See `SCHEMA.md` §4.
3. **Symbol-based anchors** (`path#Symbol`), **never line numbers.** Line numbers rot on every
   edit (the #1 drift source) and barely help an agent that has `Grep`. Symbols are stable,
   greppable, and universal across languages.
4. **Source-derived, not ticket-derived.** The KG reflects code. Tickets/PRDs are irrelevant.
5. **No separate parity file.** A node cross-links *directly* to its counterpart node in the
   other repo's KG (`counterpart`), carrying a `parity` status + one-line `divergence`. Detail
   stays on each side (no duplication); "find all gaps" = query the flags across both KGs.
6. **Point, don't copy.** Never paste code into the KG — reference symbols.
7. **Comprehensive updates.** Every refresh updates *all* affected nodes (add new, edit changed,
   remove deleted) + edges. A header/date-only "refresh" is the anti-pattern that causes drift —
   it is forbidden (`SCHEMA.md` §6).
8. **Advisory, never blocking.** Freshness/drift/validation surface as advice; they never gate a
   commit, build, or tool. (Same posture as the a11y plugin.)

## Resolved open questions

The plan's §12 open calls, decided for this build:

| Question | Decision | Note |
|---|---|---|
| **Plugin name** | `codebase-kg` | Matches the repo. Commands `/codebase-kg:kg-*`; MCP server `codebase-kg`. |
| **Repo layout** | Standalone plugin at root | `plugin.json` at root + a thin `marketplace.json` so it installs. Not a multi-plugin marketplace. |
| **KG location** | Per-repo `KNOWLEDGE_GRAPH.md`, location configurable | Repo root or `knowledge/`; set via `kg_path` in `codebase-kg.local.md`. |
| **Storage format** | Markdown-as-source | Diffable, greppable, human-readable; the MCP parses it. JSON/YAML machine-first rejected as heavier (revisit only if parsing is too slow at scale). |
| **Refresh engine** | Agent-driven first | The agent reads source and emits nodes — keeps it language-agnostic. Add static parsers later for speed if needed; never as the only path. |
| **Counterpart direction** | Reciprocal | Both sides link; `kg-validate` checks consistency and flags one-directional or dangling links. |

## Genericity rules (do not violate)

- **No language hardcoding** anywhere in skills/MCP. Source reading is agent-driven → handles any
  language. `kind` is free text.
- **Symbol discovery via Grep always**; ctags/LSP only when present — never assume a toolchain.
- **All per-repo specifics live in config** (`SCHEMA.md` §7), never in the generic components.

## Principles carried from the author's existing plugins

- **Advisory, never blocking** (the a11y-plugin rule — hard gates cause workarounds).
- **Source over tickets** — the KG is a code mirror, verified against source.
- **Comprehensive updates** — header-only refresh is forbidden; a freshness check can't verify
  node completeness, so the skill (and the author) must.

## Dogfood target

The **Acme iOS ↔ Android** pair (`BUILD_PLAN.md` §9):

- **Android** `acme-android` — `knowledge/KNOWLEDGE_GRAPH.md` was audited + refreshed to
  current source on 2026-06-08. Use it as the reference node style; it needs symbol anchors +
  parity/counterpart fields added to reach this schema.
- **iOS** `acme-ios` — ships a *narrow* Quartz-only `KNOWLEDGE_GRAPH.md`. Plan: rename
  it `KNOWLEDGE_GRAPH.legacy.md` (preserve-but-deletable), then `kg-build` the real one in place.
- Known cross-codebase cases to reproduce (already modeled in `docs/examples/EXAMPLE_KG.md`):
  matched (SavedArticle ↔ SavedArticleEntity), divergent (PersonalizedRankingService ↔
  FeedRanker), android-only (Night Digest — iOS has it only in `AcmeApp/PRD.md`).

> The throwaway `PARITY_GRAPH.md` prototype (in the Android repo) validated the parity format,
> then the design moved to **direct node cross-linking** (no separate parity file). The prototype
> can be discarded; its lessons are in decisions #5 and `SCHEMA.md` §8.

# Changelog

All notable changes to the `codebase-kg` plugin.

## [Unreleased]

### Fixed — 2026-07-03 — code-review findings: live-reloading server, push-accurate gate, loader hardening

- **MCP server reloads the KG (H1).** The KG path was resolved once at startup and the parsed
  graph/peer cached forever — a repo with no KG at session start errored on every tool call even
  after `/codebase-kg:build`, and `kg_validate` after an edit validated the pre-edit snapshot. Now
  the path is re-resolved while unresolved, and both the graph and the peer KG are re-read whenever
  the file's mtime/size changes. Module docstring + `mcp/README.md` reconciled. +2 tests.
- **MCP server honors `.claude/codebase-kg.local.md` `kg_path` (M7).** SCHEMA.md §1/§7 said the
  per-clone override applied to "the tools", but only the hooks and the pre-push gate read it. The
  server's walk-up resolution now checks the `.local.md` frontmatter at each level too. +1 test.
- **Pre-push gate reads git's stdin (M3).** The gate guessed the diff range from the checked-out
  branch (`@{u}..HEAD` → `origin/main..HEAD` → `HEAD`), so pushing a different branch diffed the
  wrong changeset, a repo whose remote default isn't main/master silently passed, and two-dot
  ranges counted upstream-side changes. It now parses the
  `<local_ref> <local_sha> <remote_ref> <remote_sha>` lines git feeds on stdin and gates exactly the
  pushed refs: three-dot `remote_sha...local_sha` per ref, new branches diffed from the merge-base
  with the remote default (else every not-yet-remote commit), ref deletions skipped. A manual run
  without stdin falls back to `@{u}...HEAD` / `origin/main|master...HEAD` and **fails loudly** when
  no base exists instead of silently passing. `git-hooks/README.md`, the `pre-push` wrapper, and
  `/codebase-kg:install-hooks` note the stdin ordering requirement. +8 tests, verified end-to-end
  against a real repo (existing ref / new branch / deletion / manual run).
- **Anchor check understands `Type.method` (M1).** `kg_validate` grepped the anchor symbol as one
  literal word, so the SCHEMA-endorsed `File.kt#Type.method` form was false-flagged ("symbol not
  found"). Dotted symbols are now checked segment-wise. Also caches file contents per validate call
  instead of re-reading a file once per anchor. +1 test.
- **Loader strips inline `# comments` from node rows (M2).** The template showed `  # optional …`
  tails on node-table rows but the loader only stripped comments on header lines, so garbage like
  `matched    # optional — multi-codebase only` escaped into parity values. Non-prose node fields
  (everything except `summary`/`divergence`) now drop a whitespace-preceded `#` tail — anchors and
  counterparts are safe because their `#` is always glued to the path. The template's node rows
  lost their inline comments (explanation moved to an HTML comment above the block). +1 test.
- **Duplicate node ids are reported (M5).** Duplicate ids silently collapsed (last wins) while
  `kg_validate` said ok, violating SCHEMA.md §4. The `Graph` now records collisions
  (`duplicate_ids`) and `kg_validate` reports them (and fails `ok`). +2 tests.
- **Table separator rows no longer truncate nodes (M6).** A `| --- | --- |` row (inserted by
  Prettier/markdownlint) inside a node block flushed the node, cutting it to id-only. Separator
  rows (dashes/colons cells) are now skipped. +1 test.
- **Advisory hook command is portable (M4).** `hooks/hooks.json` hardcoded `python`, which doesn't
  exist on stock macOS/many Linux. Now `python3 <script> || python <script>` — works under both
  POSIX `sh` and Windows `cmd.exe`, and the double-run objection can't apply because the script is
  fail-safe (always exits 0). Choice documented in `hooks/README.md`.
- **Minor:** pre-push `kg_path.lstrip("./")` charset-strip → `removeprefix("./")`; legacy
  `last refreshed` regex gains `(?i)` to match its sibling; the advisory hook's `exclude_ext` now
  matches dotfiles like `.gitignore` (aligned with the pre-push twin); `kg_validate`'s
  `root.strip("/")` no longer mangles roots (trailing slashes only; SCHEMA.md §3 + template now say
  `root` is repo-relative); `/codebase-kg:install-hooks` references the vendored files via
  `${CLAUDE_PLUGIN_ROOT}/git-hooks/…`; `docs/DESIGN.md` stale `/codebase-kg:kg-*` command names
  corrected; dead `_HEADER_KEYS` removed from the loader; `.claude-plugin/plugin.json` gains
  `"version": "0.1.0"` (matching `mcp/pyproject.toml`); `_suggest` sorts by `(-score, id)` so the
  id tiebreak is no longer reversed; the five skills drop the nonstandard `when_to_use` frontmatter
  key (unique bits folded into `description`). 36 → 52 tests, all passing.

### Changed — 2026-06-29 — `knowledge/` is the single KG location (no repo-root fallback)

- The KG **always** lives at `knowledge/KNOWLEDGE_GRAPH.md`. The previous repo-root *fallback* is
  removed everywhere: the `kg-build` skill now unconditionally creates the `knowledge/` folder and
  writes there, and all three resolvers (advisory hook `find_kg`, pre-push gate `find_kg_rel`, MCP
  server `_resolve_graph_path`) resolve a single rule — explicit `kg_path` override → else
  `knowledge/KNOWLEDGE_GRAPH.md` — with no root candidate. `kg_path` now **defaults to**
  `knowledge/KNOWLEDGE_GRAPH.md` (was empty/auto-discover), so everything points to one configurable
  path. A repo without that file simply has no KG yet (hook + gate no-op). `SCHEMA.md` §1/§7, the
  `kg-build` skill, `DESIGN.md`, the config template, and the MCP/server docs updated. 36 tests pass.
  **Migration:** a repo with a root-level `KNOWLEDGE_GRAPH.md` must move it to `knowledge/` (or set
  `kg_path` in `.claude/codebase-kg.local.md`); it is no longer discovered at the root.

### Removed — 2026-06-29 — completed design docs (`docs/BUILD_PLAN.md`, `docs/MCP_SURFACE.md`)

- Deleted the 6-phase build plan and the Phase-2 MCP-surface design spec now that all phases ship.
  Their still-relevant content lives in the authoritative docs: locked decisions + dogfood target in
  `docs/DESIGN.md`, the live tool surface in `mcp/README.md`, and the schema in `SCHEMA.md`. Fixed
  the inbound references in `README.md`, `docs/DESIGN.md`, and `docs/examples/EXAMPLE_KG.md`; the
  earlier dated changelog entry that lists both files is left as the historical record.

### Fixed — 2026-06-14 — `CODEBASE_KG_PATH` is now optional (server auto-discovers the KG)

- `.mcp.json` referenced `${CODEBASE_KG_PATH}` as a **required** env var, so Claude Code refused to
  launch the MCP server whenever it was undefined (`MCP server codebase-kg invalid: Missing
  environment variables: CODEBASE_KG_PATH`). Changed to an empty default (`${CODEBASE_KG_PATH:-}`):
  the var stays an **optional override**, and when unset the server falls through to its `knowledge/`
  walk-up auto-discovery — so the KG is found in any repo with **no per-project config**. The
  documented `CLI arg → $CODEBASE_KG_PATH → walk-up` resolution order is unchanged.

### Changed — 2026-06-09 — project config lives in the committed KG header (not a `.local.md`)

- The shared, project-level config (`codebase`/`root`/`counterpart`) is the **committed KG header**.
  The pre-push gate and the advisory hook now read `root` from the header (auto-discovering the KG),
  so **no committed config file is needed**. `.claude/codebase-kg.local.md` is now correctly an
  **optional, gitignored per-developer override** (by the `.local` convention) — used only to
  override the header for one clone. Resolves the contradiction of committing a `.local` file.
  `SCHEMA.md` §7, the config template, the `install-hooks` command, and the git-hooks/hooks READMEs
  updated. +1 test (36 passing).

### Changed — 2026-06-09 — `knowledge/` is the default KG location

- The default KG location is now **`knowledge/KNOWLEDGE_GRAPH.md`** (a `knowledge/` subdirectory),
  not the repo root — a consistent home for the KG and related committed reference docs.
  Auto-discovery in the MCP server, the pre-push gate, and the advisory hook now prefers
  `knowledge/` and falls back to repo root. `SCHEMA.md`, the `kg-build` skill, `README.md`, and the
  config template updated. (Existing root-level KGs still work via the fallback + explicit `kg_path`.)

### Added — 2026-06-09 — pre-push KG gate (`git-hooks/`)

- A blocking, **stdlib-only, vendorable** git pre-push hook (`git-hooks/kg_pre_push.py` + `pre-push`
  wrapper) that rejects a push when tracked source under the KG's `root` changed but
  `KNOWLEDGE_GRAPH.md` isn't in sync (not in the changeset, or header `refreshed:`/legacy
  `last refreshed` not today). Override: `git push --no-verify`. No dependency on the MCP package, so
  it runs for every clone/CI. The semantic update stays the agent's `/codebase-kg:refresh`.
- `/codebase-kg:install-hooks` command — agent-guided, non-destructive install (vendors the checker,
  wires `core.hooksPath`/`pre-push`, integrates into an existing hook rather than overwriting it).
- +10 tests (35 passing). This is the **enforcement** layer complementing the in-session nudge; the
  two linked repos stay eventually consistent because refresh reconciles parity vs the peer KG.

### Added — 2026-06-08 — per-node `updated` date

- New optional per-node `updated: YYYY-MM-DD` field (finer-grained than the header `refreshed`).
  `kg-build` stamps every node; `kg-refresh` bumps only touched nodes, so a node whose `updated`
  lags `refreshed` is a candidate stale node. Loader parses it (+ `last_updated`/`last-updated`
  aliases); `kg_node` returns it, `kg_search` shows it, `kg_stats` reports
  `updated.{oldest,newest,missing,stale_vs_refreshed}`. Schema/template/skills updated. +3 tests.

### Added — 2026-06-08 — Phases 2–6

- **Phase 2 — MCP query server** (`mcp/`): FastMCP stdio server parsing `KNOWLEDGE_GRAPH.md` into a
  queryable graph; 7 tools (`kg_search` / `kg_node` / `kg_neighborhood` / `kg_find_by_kind` /
  `kg_parity_gaps` / `kg_stats` / `kg_validate`). `loader`+`tools` are stdlib-only with 21 passing
  tests over a cross-linked ios/android fixture pair + source tree. Loader tolerates legacy
  hand-written field names (parses a real 700-line Android KG, 98 nodes). Root `.mcp.json`.
- **Phase 3 — skills + commands**: five advisory skills (`kg-build`, `kg-refresh`, `kg-audit`,
  `kg-link`, `kg-validate`) with references for the multi-agent ones; five thin slash commands.
- **Phase 4 — advisory hook** (`hooks/`): PostToolUse freshness nudge; never blocks, fail-safe,
  state in OS temp; honors `codebase-kg.local.md`.
- **Phase 5 — cross-codebase parity**: counterpart resolution + reciprocity in `kg_validate`,
  `kg_parity_gaps`, and the `kg-link` skill — verified end-to-end on the fixture pair.
- **Phase 6 — dogfood** (`docs/DOGFOOD.md`): read-only validation against a real Android KG;
  live migration of the external repos staged as a go-ahead step.
- `.gitattributes` (LF normalization).

### Reviewed — 2026-06-08 — plugin-dev validator + skill-reviewer

- Ran the `plugin-dev:plugin-validator` and `plugin-dev:skill-reviewer` agents — both PASS.
- **Fixed:** MCP server now loads the graph **lazily**, so it starts cleanly in a repo that has no
  `KNOWLEDGE_GRAPH.md` yet (the `/codebase-kg:build` first-run case) — a missing KG surfaces on first
  tool call, not as a server that refuses to start.
- **Sharpened** the `kg-validate` (structural/deterministic) vs `kg-audit` (semantic/deep) trigger
  descriptions so generic "check my KG" queries route deterministically.
- **Genericized** the kg-audit anecdote (no repo-specific reference) and two micro-wordings.

### Added — 2026-06-08 — Phase 1 scaffold + schema

- Repo skeleton (standalone plugin at root): `.claude-plugin/plugin.json` + thin
  `marketplace.json`, `.gitignore`, MIT `LICENSE`, `README.md`, `CHANGELOG.md`.
- **`SCHEMA.md`** — the canonical KG spec: generic node shape
  (`id / kind / anchors / summary / edges / parity / counterpart / divergence`), symbol-anchor
  rule (`path#Symbol`, never line numbers), document structure, cross-codebase parity model, and
  the no-header-only-refresh update policy.
- **`docs/examples/EXAMPLE_KG.md`** — a hand-built iOS↔Android slice exercising all three parity
  shapes (matched / divergent / android-only) with reciprocal `counterpart` links.
- `docs/DESIGN.md` (locked decisions, resolved open questions, genericity rules, principles),
  `docs/MCP_SURFACE.md` (Phase 2 tool-surface design), `docs/BUILD_PLAN.md` (the full 6-phase plan).
- `templates/KNOWLEDGE_GRAPH.template.md` (empty-KG template) +
  `templates/codebase-kg.local.md.example` (per-repo config).
- Phase placeholders with design notes: `mcp/` (Phase 2), `skills/` (Phase 3), `hooks/` (Phase 4).

### Not yet built

- Phase 2 — MCP query server (`mcp/`) + root `.mcp.json`.
- Phase 3 — skills (`kg-build` / `kg-refresh` / `kg-audit` / `kg-link` / `kg-validate`).
- Phase 4 — advisory post-edit freshness hook.
- Phase 5 — `counterpart` resolution + `kg_parity_gaps`.
- Phase 6 — dogfood on a real iOS↔Android pair.

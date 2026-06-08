# Changelog

All notable changes to the `codebase-kg` plugin.

## [Unreleased]

### Added — 2026-06-08 — Phases 2–6

- **Phase 2 — MCP query server** (`mcp/`): FastMCP stdio server parsing `KNOWLEDGE_GRAPH.md` into a
  queryable graph; 7 tools (`kg_search` / `kg_node` / `kg_neighborhood` / `kg_find_by_kind` /
  `kg_parity_gaps` / `kg_stats` / `kg_validate`). `loader`+`tools` are stdlib-only with 21 passing
  tests over a cross-linked ios/android fixture pair + source tree. Loader tolerates legacy
  Acme-Android field names (parses the real 700-line Android KG, 98 nodes). Root `.mcp.json`.
- **Phase 3 — skills + commands**: five advisory skills (`kg-build`, `kg-refresh`, `kg-audit`,
  `kg-link`, `kg-validate`) with references for the multi-agent ones; five thin slash commands.
- **Phase 4 — advisory hook** (`hooks/`): PostToolUse freshness nudge; never blocks, fail-safe,
  state in OS temp; honors `codebase-kg.local.md`.
- **Phase 5 — cross-codebase parity**: counterpart resolution + reciprocity in `kg_validate`,
  `kg_parity_gaps`, and the `kg-link` skill — verified end-to-end on the fixture pair.
- **Phase 6 — dogfood** (`docs/DOGFOOD.md`): read-only validation against the real Acme Android KG;
  live migration of the Acme repos staged as a go-ahead step.
- `.gitattributes` (LF normalization).

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
- Phase 6 — dogfood on the Acme iOS↔Android pair.

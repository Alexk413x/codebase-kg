# hooks/ — Phase 4 (not built yet)

A single **advisory** post-edit freshness nudge. Mirrors the a11y plugin's `hooks/` (a `hooks.json`
+ Python scripts run via `python ${CLAUDE_PLUGIN_ROOT}/hooks/...`).

Planned behavior:

- **PostToolUse** on `Edit|Write|MultiEdit`: when a source file under the configured `root` changes
  but the repo's `KNOWLEDGE_GRAPH.md` wasn't touched in the same stretch of work, surface a quiet
  reminder — "source changed; the KG may need a node update (run `kg-refresh`)."
- **Never blocking.** No commit/build gate. Honors per-repo config (`codebase-kg.local.md`) and an
  inline opt-out (e.g. a `kg-ignore` marker), exactly like the a11y hooks' override posture.

This is a *nudge*, not enforcement — the comprehensive-update contract (`SCHEMA.md` §6) is the
author's responsibility; a hook can only notice that the KG file went untouched, not whether the
nodes are complete.

Design notes: `BUILD_PLAN.md` §4, and decision #8 in `docs/DESIGN.md` (advisory, never blocking).

# skills/ — Phase 3 (not built yet)

Five skills, each a `SKILL.md` (frontmatter: `name`, `description`, `when_to_use`, `effort`,
`allowed-tools` — mirror the a11y plugin's skills). All are **advisory** and **source-derived**.

| Skill | Purpose |
|---|---|
| `kg-build` | Bootstrap a KG from scratch: read the source tree, emit nodes per `SCHEMA.md`. Parallelize with Explore-style sub-agents per subsystem. |
| `kg-refresh` | Re-derive against current source; add/edit/remove nodes **comprehensively** + edges; bump `refreshed`. Enforces the no-header-only rule (`SCHEMA.md` §6). |
| `kg-audit` | Source-vs-KG drift sweep (the proven 4-agent pattern: partition the KG, verify each node's anchors/claims against source, report STALE / MISSING / INACCURATE). Advisory output. |
| `kg-link` | Establish/maintain cross-codebase `counterpart` + `parity` + `divergence` on nodes, reading **both** codebases. |
| `kg-validate` | Run the MCP `kg_validate` and report (dangling edges, dangling/one-directional counterparts, ungreppable anchors). |

Design notes: `BUILD_PLAN.md` §6, `SCHEMA.md`. The build/refresh skills write markdown KG tables;
the audit/validate skills read + report, never block.

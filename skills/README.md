# skills/

Five skills, all **advisory** and **source-derived**. Each is a `SKILL.md` (third-person trigger
description + imperative workflow), with the heavy multi-agent procedures pushed into `references/`.

| Skill | Use when | Writes? |
|---|---|---|
| [`kg-build`](kg-build/SKILL.md) | a repo has no KG (or too narrow a one) — bootstrap from source | writes the KG |
| [`kg-refresh`](kg-refresh/SKILL.md) | code changed — re-sync the affected nodes (never header-only) | edits the KG |
| [`kg-audit`](kg-audit/SKILL.md) | deep read-only accuracy sweep — STALE / INACCURATE / MISSING | report only |
| [`kg-link`](kg-link/SKILL.md) | establish/maintain cross-codebase parity (counterpart links) | edits both KGs' parity fields |
| [`kg-validate`](kg-validate/SKILL.md) | cheap deterministic structural drift check | report only |

All five lean on the MCP query surface (`mcp__codebase-kg__*`) and the schema (`../SCHEMA.md`).
Build/refresh write KG markdown; audit/validate read + report; link maintains parity across a pair.

Division of labor: **kg-validate** is structural and deterministic (dangling edges, ungreppable
anchors, parity field shape); **kg-audit** is semantic (does the summary still match the source?);
**kg-refresh** is the only one that fixes drift in place.

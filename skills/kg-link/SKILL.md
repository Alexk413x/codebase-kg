---
name: kg-link
description: This skill should be used when the user asks to "link two knowledge graphs", "set up parity between iOS and Android", "find feature gaps between the apps", "add counterpart links", "map parity across codebases", or wants to track what matches/diverges between a codebase and its port. It reads BOTH codebases' KGs and source, then sets reciprocal counterpart + parity + divergence fields.
when_to_use: Use to establish or maintain cross-codebase parity between two repos that each have a KG (e.g. an app and its platform port). Requires read access to both codebases. For single-codebase work use kg-build / kg-refresh.
allowed-tools:
  - mcp__codebase-kg__kg_parity_gaps
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_stats
  - Read
  - Grep
  - Glob
  - Bash(git ls-files:*)
  - Write
  - Edit
---

# kg-link — cross-codebase parity (counterpart linking)

Connect two repos' KGs so feature parity is **queryable**. Parity is expressed by **direct node
cross-linking** (SCHEMA.md §8) — no separate parity file. Each node gains, where applicable:
`parity` (matched / divergent / `<codebase>-only`), `counterpart` (a link to the peer node), and
`divergence` (one line, only when divergent). Detail stays in each side's own `summary`; this skill
adds the links, never duplicates the content.

**Descriptive, not prescriptive.** It records what matches and what diverges. It does not assign
work or decide which side is "right."

## Prerequisites

- Both repos have a KG (run `kg-build` on each side first).
- Both codebases are readable locally. The peer KG path is the header `counterpart:` (or
  `codebase-kg.local.md`). Note: a port may only be *readable* on this OS (e.g. iOS source on
  Windows) — reading is all this skill needs.

## Workflow

### 1. Pair the headers
Confirm each KG's header `counterpart:` points at the other (reciprocal). If missing, add it.

### 2. Match nodes across the two KGs
Walk one side's nodes and find each one's peer on the other side — by concept, not by name. Match on
behavior/role (a "bookmarks list screen" ↔ a "bookmarks list screen"), tolerating expected platform
tooling differences (SwiftUI↔Compose, SwiftData↔Room, Combine↔Flow, actor↔Dispatchers). Use
`kg_search` on each side to find candidates. Record three outcomes per node:
- **a clear peer** → it's `matched` or `divergent` (decide in step 3).
- **no peer in code** → `<codebase>-only` (verify against *source*, not a PRD/ticket — a feature
  mentioned only in docs is still `<codebase>-only`).

### 3. Classify matched vs divergent (read source on both sides)
For a matched pair, open the anchored source on **both** sides and compare data shape, logic,
placement, and wiring:
- Equivalent implementation (allowing platform tooling) → **matched**.
- Materially different — different data shape, score formula, persistence, or one side unwired →
  **divergent**, with a one-line `divergence:` naming the difference. Keep the detail in each side's
  `summary`; the divergence line is just the headline.

### 4. Write reciprocal links
On each node, set `parity`, `counterpart` (`<peer-kg-relative-path>#<peer-node-id>`), and
`divergence` (divergent only). Make links **reciprocal**: if A→B, then B→A. A `<codebase>-only` node
gets the flag and **no** counterpart.

### 5. Validate reciprocity
Run `kg_validate` on each side (with the peer loaded). Fix every "not reciprocal" /
"counterpart id not in peer" finding. Then `kg_parity_gaps` to produce the gap report.

## The gap report

`kg_parity_gaps` is the payoff — the "find all gaps" query, not a maintained file:

```
## Parity gaps — <codebaseA> ↔ <codebaseB>
divergent (<n>):
- <node-id> ↔ <peer-id>: <divergence line>
<codebaseA>-only (<n>):
- <node-id>: <one-line what it is>  (no peer in <codebaseB> source)
<codebaseB>-only (<n>):
- <peer-id>: <one-line what it is>  (no peer in <codebaseA> source)

Verdict: <N divergences, M platform-only features>. Observation only — no work assigned.
```

## Rules

- **Reciprocal or it's a bug.** Every counterpart link has a back-link; `kg_validate` enforces it.
- **Source-verified `*-only`.** Before flagging a feature platform-only, grep the *other* codebase to
  confirm it's truly absent in code (not just named differently).
- **No duplication.** The counterpart link points; it does not copy the peer's summary.
- **Observation only.** Surfacing a gap is not proposing work — leave ticketing decisions off the KG.

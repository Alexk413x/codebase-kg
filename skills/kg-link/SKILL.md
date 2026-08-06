---
name: kg-link
description: This skill should be used when the user asks to "link two code graphs", "set up parity between iOS and Android", "find feature gaps between the apps", "add counterpart links", "map parity across codebases", or wants to track what matches/diverges between a codebase and its port (both repos must have a code_graph.db and be readable). It reads BOTH codebases' graphs and source, then sets reciprocal counterpart + parity + divergence fields. (For single-codebase work use kg-build / kg-refresh.)
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
  - Bash(python -m codebase_kg.export:*)
  - Bash(python -m codebase_kg.build:*)
  - mcp__codebase-kg__kg_upsert_node
  - mcp__codebase-kg__kg_add_link
  - mcp__codebase-kg__kg_remove_link
  - Write
  - Edit
---

# kg-link — cross-codebase parity (counterpart linking)

Connect two repos' graphs so feature parity is **queryable**. Parity is expressed by **direct node
cross-linking** (`SCHEMA.md` §9) — no separate parity file. Each node gains, where applicable:
`parity` (matched / divergent / `<codebase>-only`), `counterpart` (a link to the peer node), and
`divergence` (one line, only when divergent). Detail stays in each side's own `description`; this
skill adds the links, never duplicates the content.

**Descriptive, not prescriptive.** It records what matches and what diverges. It does not assign
work or decide which side is "right."

## Prerequisites

- Both repos have a `code_graph.db` (run `kg-build`, or migrate, on each side first).
- Both codebases are readable locally. The peer path is `meta.counterpart` (or
  `.claude/codebase-kg.local.md`). A port may only be *readable* on this OS (e.g. iOS source on
  Windows) — reading is all this skill needs.

## The three legal shapes — the store enforces them

A build fails rather than storing a half-filled parity triple, so get these right up front:

| `parity` | `counterpart` | `divergence` |
|---|---|---|
| `matched` | **required** | must be absent |
| `divergent` | **required** | **required** |
| `<codebase>-only` | must be absent | must be absent |

## Workflow

A parity sweep is **bulk** work — it touches most of both graphs at once and the reviewable diff is
the deliverable — so edit each side through its JSON:

```sh
python -m codebase_kg.export -o .kg-export.json
#   … set parity / counterpart / divergence …
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json
```

Both sides change, so expect two export/build cycles.

Use `kg_upsert_node` only for **touching up a few nodes afterwards** — one pair that turned out to
be divergent, one flag that was wrong. It writes the same three fields (`parity`, `counterpart`,
`divergence`), atomically, and `null` clears one. It re-validates the graph before landing, which
means it will refuse a link the peer graph does not reciprocate — the check that makes parity worth
having, arriving before the write rather than in the next `kg_validate` run.

### 1. Pair the config
Confirm each graph's `meta.counterpart` points at the other's `code_graph.db` (reciprocal). If
missing, add it.

### 2. Match nodes across the two graphs
Walk one side's nodes and find each one's peer on the other side — **by concept, not by name**.
Match on behavior/role (a "bookmarks list screen" ↔ a "bookmarks list screen"), tolerating expected
platform tooling differences (SwiftUI↔Compose, SwiftData↔Room, Combine↔Flow, actor↔Dispatchers).
Use `kg_search` on each side to find candidates.

- **a clear peer** → `matched` or `divergent` (decide in step 3).
- **no peer in code** → `<codebase>-only`. Verify against *source*, not a PRD or ticket — a feature
  that exists only in docs is still `<codebase>-only`.

### 3. Classify matched vs divergent (read source on both sides)
For a matched pair, open the anchored source on **both** sides and compare data shape, logic,
placement, and wiring:

- Equivalent implementation (allowing platform tooling) → **matched**.
- Materially different — different data shape, score formula, persistence, or one side unwired →
  **divergent**, with a one-line `divergence` naming the difference. Keep detail in each side's
  `description`; the divergence line is the headline.

### 4. Write reciprocal links
On each node set `parity`, `counterpart` (`<peer-graph-relative-path>#<peer-node-id>`, e.g.
`../../acme-ios/knowledge/code_graph.db#saved_article`), and `divergence` (divergent only). Make
links **reciprocal**: if A→B then B→A.

### 5. Build both sides, then validate reciprocity
Build each graph. Run `kg_validate` on each side (the peer is opened automatically from
`meta.counterpart`). Fix every "not reciprocal" / "counterpart id not in peer graph" finding. Then
`kg_parity_gaps` for the report.

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

- **Reciprocal or it's a bug.** Every counterpart link has a back-link; `kg_validate` reports the
  ones that don't.
- **Source-verified `*-only`.** Before flagging a feature platform-only, grep the *other* codebase to
  confirm it's truly absent in code (not just named differently).
- **No duplication.** The counterpart link points; it does not copy the peer's description.
- **Observation only.** Surfacing a gap is not proposing work — leave ticketing decisions out.

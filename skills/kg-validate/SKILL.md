---
name: kg-validate
description: This skill should be used when the user asks to "validate the code graph", "check the KG for drift", "lint code_graph.db", "find broken anchors", or "check the parity links" for a repo that already has a graph. It runs the fast, deterministic codebase-kg drift checks — anchors that no longer resolve to source, source files no node covers, and broken or non-reciprocal counterpart links — and reports. Advisory only, never blocking. The cheap pre-check before the deeper kg-audit. (For the deep SEMANTIC accuracy sweep against source, use kg-audit instead.)
allowed-tools:
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - Read
  - Grep
  - Bash(git diff:*)
---

# kg-validate — deterministic drift check (advisory)

Run the codebase-kg validator over a repo's `knowledge/code_graph.db` and report. This is the
**cheap, deterministic** drift pre-check; `kg-audit` is the deeper source-vs-claim sweep.
**Advisory only — it never blocks a commit, build, or tool.**

## What it checks

Everything here is a question the file cannot answer about itself — it needs the source tree:

- **Ungreppable anchors** — a `path#Symbol` whose file is gone, or whose symbol no longer appears in
  it. The strongest signal a node has gone stale.
- **Uncovered sources** — files under `root` that no node anchors on. Usually new code nobody
  mapped. The check is language-agnostic: "source" means the extensions this graph already anchors
  on, so a Swift repo looks for `.swift` without anything being hardcoded.
- **Counterpart problems** — a `counterpart` whose target file or id is missing, or that the peer
  graph doesn't link back to (a reciprocity break — `SCHEMA.md` §9).
- **Description violations** — a description carrying a ticket ref, a date, or change narrative.
  Should be empty: the builder rejects these. A hit means the file was written by something else.

## What it no longer checks, because it cannot happen

Do not go looking for these, and do not report their absence as a clean bill of health for something
that was never at risk:

| Old finding | Now |
|---|---|
| duplicate node ids | primary key — unwritable |
| dangling edges | foreign key — unwritable |
| orphan anchors | foreign key — unwritable |
| `matched` with no counterpart, `divergent` with no divergence, `*-only` with a counterpart | CHECK constraints — unwritable |
| line-number anchors | CHECK constraint — unwritable |

`kg_validate` returns these as `guaranteed_by_schema` so the report can say *why* they are absent.

## Workflow

1. **Locate the graph.** If the user named a file, use it. Otherwise the MCP server auto-discovers
   `knowledge/code_graph.db` (the only location — no repo-root fallback). Confirm with `kg_stats` —
   note the `generated` date and node count.
2. **Run `kg_validate`.** It returns `anchor_issues`, `coverage`, `changed_since_built`,
   `counterpart_issues`, `description_issues`, plus `source_checked` (whether the source tree was
   reachable) and `anchors_checked`.
3. **Check `coverage.declared` first.** If it is `false`, the coverage answer is *incomplete* — the
   graph has no `covers`, so file types it has never mapped were not looked at. Report that as the
   headline finding, not a footnote: a clean coverage number from an undeclared graph means nothing.
4. **Report** in the format below. Sort by severity: ungreppable anchors first (they break
   navigation), then coverage gaps, then drift, then counterpart issues.
5. **Recommend, do not act.** Point each finding at its fix — `kg-refresh` for stale anchors and
   coverage gaps, `kg-link` for counterpart issues. Do not edit the graph from this skill unless the
   user asks.

## Report format

```
## Code graph validation — <codebase> (generated <date>, <N> nodes)
Source check: <ran against source | skipped — source root not found>
Anchors checked: <N>

Ungreppable anchors (<n>):
- <node-id>: `<path#Symbol>` — <file not found | symbol not found in file>  → run kg-refresh

Coverage: <N covered, N gaps, N exempt, N out of scope>   [declared | NOT DECLARED]
- <path> — in `covers`, no node anchors it  → run kg-refresh

Changed since built (<n>):
- <node-id>: `<path>` — source edited after this description was written  → re-read and confirm

Counterpart issues (<n>):
- <node-id>: <not reciprocal | target id not in peer graph | file not found>  → run kg-link

Guaranteed by the store (not checked, cannot occur): unique ids, no dangling edges,
consistent parity, symbol-only anchors.

Verdict: <clean | N advisory findings — none blocking>
```

If `kg_validate` returns `ok: true`, say so — but qualify it honestly. `ok` covers structure and
coverage, **not** accuracy: it means the anchors resolve and nothing in `covers` is unmapped. It does
not mean the descriptions are still true. Say "clean against source as of `<generated>`", and if
`changed_since_built` is non-zero name that number in the same breath.

## Notes

- A skipped source check (`source_checked: false`) is not a failure — it means anchor paths couldn't
  be resolved to files (wrong `root`, or source not checked out). Say so; don't imply the anchors
  are fine.
- `coverage.gaps` is capped at 50 entries; `coverage.truncated` says when the cap was hit. Report it
  as truncated rather than reporting 50 as the total.
- `changed_since_built` is deliberately **not** part of `ok`. A changed file is a prompt to re-read,
  not a defect — treat it that way in the report. `unhashed` counts files with no recorded baseline
  (a graph built without source in reach); those are unknown, not unchanged.
- This skill reads; it does not write. Drift is surfaced as advice.

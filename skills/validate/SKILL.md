---
name: validate
description: Fast, deterministic drift check on a repo that has a code graph. It reports anchors that no longer resolve, source files no node covers, mapped files changed since the graph was built, and broken or one-way counterpart and external links. Advisory; it blocks nothing. Use when the user asks to "validate the code graph", "lint code_graph.db", "run kg_validate", "find broken anchors" or "check the parity links", or wants a cheap check before committing. (For the deep check that re-reads source and judges whether descriptions are still true, use audit.)
allowed-tools:
  # Both names the host gives the server — bare when the MCP server is installed
  # directly, prefixed when it arrives as a plugin.
  - mcp__codebase-kg__kg_validate
  - mcp__codebase-kg__kg_stats
  - mcp__plugin_codebase-kg_codebase-kg__kg_validate
  - mcp__plugin_codebase-kg_codebase-kg__kg_stats
  - Read
---

# validate — deterministic drift check (advisory)

Run the codebase-kg validator over a repo's `knowledge/code_graph.db` and report. This is the
**cheap, deterministic** drift pre-check; `audit` is the deeper source-vs-claim sweep.
**This skill is advisory — it never blocks a commit, a build, or a tool call.** (The plugin's search
gate does deny searches until a graph query earns credit; that is a separate component and not this
one.)

## What it checks

Everything here is a question the file cannot answer about itself — it needs the source tree:

- **Ungreppable anchors** — a `path#Symbol` whose file is gone, or whose symbol no longer appears in
  it. The strongest signal a node has gone stale.
- **Uncovered sources** — files under `root` that `covers` says should be mapped and nothing
  anchors. Scope is **declared**, not inferred: `meta.covers` states what counts and `meta.exempt`
  subtracts what is deliberately left out. A graph with no `covers` falls back to "the extensions
  this graph already anchors on", which cannot see a file type nobody has ever covered — that is
  why step 3 checks `coverage.declared` before reading the number.
- **Digest drift** (`changed_since_built`) — mapped files whose contents no longer match the SHA-256
  recorded when the graph was built (`${CLAUDE_PLUGIN_ROOT}/SCHEMA.md` §6.3). The anchor still resolves, so nothing else
  notices; the description may no longer fit. Deliberately outside `ok` — a changed file is a prompt
  to re-read, not a failure. Absent baselines report as `unhashed`, which means "no baseline", never
  "unchanged".
- **Counterpart problems** — a `counterpart` whose target file or id is missing, or that the peer
  graph doesn't link back to (a reciprocity break — `${CLAUDE_PLUGIN_ROOT}/SCHEMA.md` §9).
- **External link problems** (`external_link_issues`) — a link that is malformed, or that names a
  node the peer graph does not contain. Report these: the `error`-severity ones count against `ok`,
  so skipping them lets you report "clean" over a payload that says `ok: false`. The `warning`-
  severity ones (peer absent or unreadable) are unknown, not broken.
- **Reference problems** (`reference_issues`) — a documentation reference whose `path` or `symbol`
  is not one of its node's own anchors. It counts against `ok`. The usual cause is an anchor that
  moved while the reference that narrowed to it did not.
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
   `staleness`, `counterpart_issues`, `description_issues`, `external_link_issues`,
   `reference_issues`, plus
   `source_checked`
   (whether the source tree was reachable), `source_base` (the directory anchors resolved from) and
   `anchors_checked`. Report `external_link_issues` — its `error`-severity entries count against
   `ok`, so skipping them lets you call a graph clean over a payload that says `ok: false`.
3. **Check `coverage.declared` first.** If it is `false`, the coverage answer is *incomplete* — the
   graph has no `covers`, so file types it has never mapped were not looked at. Report that as the
   headline finding, not a footnote: a clean coverage number from an undeclared graph means nothing.
4. **Report** in the format below. Sort by severity: ungreppable anchors first (they break
   navigation), then coverage gaps, then drift, then counterpart and external link issues.
5. **Recommend, do not act.** Point each finding at its fix — `refresh` for stale anchors and
   coverage gaps, `link` for counterpart issues. Do not edit the graph from this skill unless the
   user asks.

## Report format

```
## Code graph validation — <codebase> (generated <date>, <N> nodes)
Source check: <ran against source | skipped — source root not found>
Anchors checked: <N>

Ungreppable anchors (<n>):
- <node-id>: `<path#Symbol>` — <file not found | symbol not found in file>  → run refresh

Coverage: <N covered, N gaps, N exempt, N out of scope>   [declared | NOT DECLARED]
- <path> — in `covers`, no node anchors it  → run refresh

Changed since built (<n> anchors; <staleness.stale_files> files, <staleness.stale_nodes> nodes):
- <node-id>: `<path>` — source edited after this description was written  → re-read and confirm

Counterpart issues (<n>):
- <node-id>: <not reciprocal | target id not in peer graph | file not found>  → run link

External link issues (<n>):
- <node-id>: <target> — <malformed | names a node the peer graph does not contain>  → run link
- <node-id>: <target> — peer graph absent or unreadable (unknown, not broken)

Guaranteed by the store (not checked, cannot occur): <echo kg_validate's guaranteed_by_schema
list verbatim — do not retype it from memory, the two have drifted before>.

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
- `coverage.gaps` is capped at 50 entries; `coverage.truncated` is **present only when the cap was
  hit** — it is not a boolean that is always there. When it is present, report the list as truncated
  rather than reporting 50 as the total.
- When `coverage.declared` is false the payload ships its own `coverage.warning`. Quote it rather
  than paraphrasing, so the skill and the tool cannot drift apart.
- `changed_since_built` is deliberately **not** part of `ok`. A changed file is a prompt to re-read,
  not a defect — treat it that way in the report. `unhashed` counts files with no recorded baseline
  (a graph built without source in reach); those are unknown, not unchanged.
- This skill reads; it does not write. Drift is surfaced as advice.

# hooks/

Two Claude Code hooks. Both ship with the plugin — there is **nothing to install per repo** — and
both no-op in a repo with no `knowledge/code_graph.db`.

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config — PreToolUse on `Grep\|Glob\|Bash\|PowerShell` and on the codebase-kg MCP tools → `kg_search_gate.py`; PostToolUse on those same MCP tools → `kg_search_gate.py` (that pass sizes the credit); PostToolUse on `Edit\|Write\|MultiEdit` → `kg_post_edit_check.py`. |
| `kg_search_gate.py` | The gate. Denies a search aimed at mapped code with the instruction to query the graph, and keeps doing it — a query buys credit, a located search is free, a repeat always passes. |
| `kg_post_edit_check.py` | The nudge. Two signals: the edited file isn't in the graph at all, or enough mapped files have changed since the graph was rebuilt. |
| `_config.py` | Reads `root` from the committed graph's `meta` table (auto-discovers `knowledge/code_graph.db`); a gitignored `.claude/codebase-kg.local.md` may override. Decides what counts as a source file. |

## The search gate (PreToolUse)

The only part of the plugin that can **deny** a tool call, and the only reason the graph gets used at
all. Left alone, an agent greps and re-derives the map it already has.

A `Grep` / `Glob` — or a shell `grep`, `rg`, `fd`, `find -name`, `Get-ChildItem -Recurse` — aimed at
mapped code is denied, with instructions to run `kg_search` / `kg_node` / `kg_neighborhood` first.
It **keeps asking**: an earlier version stood down for the session after one nudge, which an agent
paid once before grepping freely for the rest of the turn.

A `PreToolUse` hook cannot add an argument to `Grep`, so there is no `force` flag — and that is the
better design, since a self-declared override is a rubber stamp an agent learns to always pass.
Three ways through, each inferred from what the agent actually did:

| Way through | What it means |
|---|---|
| **A query buys credit** | A codebase-kg MCP call clears the next `gate_credit` searches. The allowance is for what the answer did NOT name — a partial answer leaves a remainder only searching will find. The files it did name are located searches, which cost nothing. |
| **A located search** | A search scoped to a file the graph already anchors is never gated and spends no credit. The agent has evidently found it; gating that buys nothing. A *directory* is still gated — that is where you look when you do not yet know the file. |
| **Repeat to insist** | The identical search, immediately after a denial, is always allowed. This is the escape hatch for code the graph has not mapped yet, and it is what makes the gate unable to strand anyone. |

- **No-ops** when the repo has no graph, when `SKIP_KG` is set, when `search_gate` is `off`, and when
  the search is scoped outside the graph's `root` or into an ignored dir (`node_modules`, `build`, …).
- **Fails open.** A malformed payload or an unreadable graph lets the search through. An unwritable
  state file degrades the deny to a **warn**, because the escape hatch lives in that file: a denial
  that cannot be recorded is one a repeat could not be recognised against.
- The grant is sized on `PostToolUse`, where the answer exists to be counted. The `PreToolUse` pass
  grants the buffer alone, so a query that errors — or one this hook cannot parse — is still worth
  something rather than nothing, and never lowers credit already held.

Shell detection is deliberately narrow — a false positive denies unrelated work. `find` and
`Get-ChildItem` only count when they carry a name/path filter, so an ordinary `find . -type d` is not
a search.

## The post-edit nudge (PostToolUse)

**Advisory.** Never blocks, never edits, fail-safe (any error exits silently so an edit is never
broken). Two signals, strongest first:

1. **The edited file is not anchored by any node.** A fact, not a guess — the map has a hole exactly
   where you're working. Reported the first time you touch that file, once per file.
2. **`nudge_every` edits to already-mapped files** (default 5). A weaker heuristic for descriptions
   drifting.

Also:

- Editing the `code_graph.db` **resets** the counter (you kept it in sync).
- **No-ops** when the repo has no graph, when `post_edit_nudge: false`, when the edited file is a
  doc/config (`.md`, `.json`, …) or lives in an ignored dir (`.git`, `node_modules`, `build`, …).

Both hooks keep state in the OS temp dir — the nudge keyed by project, the gate by project +
session. **Nothing is written into the repo.**

## Config — optional per-dev override only (gitignored `.claude/codebase-kg.local.md`)

The shared config (`root`) is the committed graph's `meta` table. This file is **only** for a
per-developer override of hook behavior, `root`, or `graph_path`; it is gitignored, not committed.
See `../templates/codebase-kg.local.md.example`.

| key | default | meaning |
|---|---|---|
| `search_gate` | `block` | `block` \| `warn` (message, no deny) \| `off` |
| `gate_shell_search` | `true` | also gate `grep`/`rg`/`find -name` run through a shell |
| `gate_credit` | `3` | searches one graph query clears |
| `post_edit_nudge` | `true` | master off-switch for the nudge |
| `nudge_every` | `5` | edits to mapped files between periodic nudges |
| `root` | from the graph | only paths under here count |
| `graph_path` | `knowledge/code_graph.db` | where the graph lives (`kg_path` still read) |

`SKIP_KG=1` in the environment silences the search gate and the commit-time staleness check for that
shell, for a change that deliberately outruns the graph.

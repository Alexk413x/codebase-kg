# hooks/

Three Claude Code hooks. All ship with the plugin — there is **nothing to install per repo** — and
all no-op in a repo with no `knowledge/code_graph.db`.

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config — PreToolUse on `Grep\|Glob\|Bash\|PowerShell` and on the codebase-kg MCP tools → `kg_search_gate.py`; PostToolUse on `Edit\|Write\|MultiEdit` → `kg_post_edit_check.py`; SessionStart on `startup\|resume\|clear` → `kg_session_start.py`. |
| `kg_search_gate.py` | The gate. Denies a search aimed at mapped code with the instruction to query the graph, and keeps doing it — a query buys credit, a located search is free, a repeat always passes. |
| `kg_post_edit_check.py` | The nudge. Two signals: the edited file isn't in the graph at all, or enough mapped files have changed since the graph was rebuilt. |
| `kg_session_start.py` | The unwired-clone notice. One line when this clone has the committed checkers but no `core.hooksPath` or no `diff.codegraph.textconv`. Prints; never writes. |
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
| **Repeat to insist** | A search for the same thing as one already denied is allowed for the rest of the session: the same call, or the same pattern (a grep pattern, a `find -name` value, a Glob pattern) however the command around it is reworded. The last 16 denials are remembered, so searches denied in parallel do not evict each other. This is the escape hatch for code the graph has not mapped yet, and it is what makes the gate unable to strand anyone. |

- **No-ops** when the repo has no graph, when `SKIP_KG` is set, when `search_gate` is `off`, and when
  the search is scoped outside the graph's `root` or into an ignored dir (`node_modules`, `build`, …).
- **Leaves non-source searches alone.** A search whose file-name filters can only match files no graph
  anchors — `find -name Info.plist`, `--include='*.xcconfig'`, `Glob **/*.png`, anything scoped
  inside an `.xcodeproj` or `.xcassets` — is not a question for the graph. The list is
  `NON_SOURCE_EXT` in `kg_search_gate.py` plus the repo's `exclude_ext`.
- **Reads shell commands the way the shell does.** A flag's value (`-A 20`, `--include x`) is not a
  path, and an unexpanded `$VAR` or `$(…)` is no evidence either way, so neither can turn a grep of one
  known file into a "still hunting" denial.
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

## The unwired-clone notice (SessionStart)

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`, which **git never
clones**. So a repo can commit the checkers, the wrappers, `install.sh` and the `.gitattributes`
line, and every fresh checkout still starts with the hooks inert and the graph diffing as "Binary
files differ" — with no error anywhere to say so. This hook is the one thing that notices.

It prints one line naming the command, and does nothing else:

```
codebase-kg: this clone is not wired for code_graph.db (git hooks and graph diffs).
git does not clone .git/config, so run:  sh .githooks/install.sh
```

**It never runs `git config`, and it must not.** Git leaves `.git/config` out of a clone on purpose:
cloning a repo must not be able to make it execute code. A plugin that set `core.hooksPath` on the
user's behalf would route around that protection and make the repo's vendored `.githooks/*.py` live
in a fresh clone without anyone choosing to run them. A plugin the user installed may suggest; it may
not decide.

Silent unless **all** of these hold:

- inside a git work tree;
- the graph (`graph_path`) exists;
- a hooks dir with both vendored checkers exists (`core.hooksPath` if set, otherwise `.githooks/`);
- `core.hooksPath` is unset, **or** `diff.codegraph.textconv` is unset.

And never when `core.hooksPath` already points somewhere other than that dir — that repo made a
deliberate choice, and nagging it toward clobbering its own config is worse than saying nothing.
`SKIP_KG` silences it like the rest. Any error exits silently; a session never fails to start
because of this.

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

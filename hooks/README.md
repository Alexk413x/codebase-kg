# hooks/

One **advisory** PostToolUse hook. Never blocks, never edits, fail-safe (any error exits silently so
an edit is never broken).

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config — PostToolUse on `Edit\|Write\|MultiEdit` → `kg_post_edit_check.py`. |
| `kg_post_edit_check.py` | The nudge. Two signals: the edited file isn't in the graph at all, or enough mapped files have changed since the graph was rebuilt. |
| `_config.py` | Reads `root` from the committed graph's `meta` table (auto-discovers `knowledge/code_graph.db`); a gitignored `.claude/codebase-kg.local.md` may override. Decides what counts as a source file. |

## Behavior

Two signals, strongest first:

1. **The edited file is not anchored by any node.** A fact, not a guess — the map has a hole exactly
   where you're working. Reported the first time you touch that file, once per file.
2. **`nudge_every` edits to already-mapped files** (default 5). A weaker heuristic for descriptions
   drifting.

Also:

- Editing the `code_graph.db` **resets** the counter (you kept it in sync).
- **No-ops** when the repo has no graph, when `post_edit_nudge: false`, when the edited file is a
  doc/config (`.md`, `.json`, …) or lives in an ignored dir (`.git`, `node_modules`, `build`, …).
- State lives in the OS temp dir (keyed by project path) — **nothing is written into the repo**.

## Config — optional per-dev override only (gitignored `.claude/codebase-kg.local.md`)

The shared config (`root`) is the committed graph's `meta` table. This file is **only** for a
per-developer override of the nudge behavior, `root`, or `graph_path`; it is gitignored, not
committed. See `../templates/codebase-kg.local.md.example`.

| key | default | meaning |
|---|---|---|
| `post_edit_nudge` | `true` | master off-switch |
| `nudge_every` | `5` | edits to mapped files between periodic nudges |
| `root` | from the graph | only edits under here count |
| `graph_path` | `knowledge/code_graph.db` | where the graph lives (`kg_path` still read) |

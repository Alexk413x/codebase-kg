# hooks/

One **advisory** PostToolUse hook. Never blocks, never edits, fail-safe (any error exits silently
so an edit is never broken).

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config — PostToolUse on `Edit\|Write\|MultiEdit` → `kg_post_edit_check.py`. |
| `kg_post_edit_check.py` | The nudge: counts source edits under `root` since the KG was last touched; when the count hits the threshold, emits a `systemMessage` reminding you to run `/codebase-kg:refresh`. |
| `_config.py` | Reads `root` from the committed KG header (auto-discovers the KG); a gitignored `.claude/codebase-kg.local.md` may override. Decides what counts as a source file. |

## Behavior

- Increments a per-project counter on each source edit under `root` (defaults to the whole repo).
- Editing the `KNOWLEDGE_GRAPH.md` **resets** the counter (you kept it in sync).
- Nudges once per `nudge_every` source edits (default 5).
- **No-ops** when the repo has no KG, when `post_edit_nudge: false`, when the edited file is a
  doc/config (`.md`, `.json`, …) or lives in an ignored dir (`.git`, `node_modules`, `build`, …).
- State lives in the OS temp dir (keyed by project path) — **nothing is written into the repo**.

## Config — optional per-dev override only (gitignored `.claude/codebase-kg.local.md`)

The shared config (`root`) is the committed KG header. This file is **only** for a per-developer
override of the nudge behavior or `root`; it is gitignored, not committed.

```yaml
---
post_edit_nudge: true     # master off-switch
nudge_every: 5            # nudge once per N source edits since the KG was last touched
root: app/src             # only edits under here count (default: whole repo)
kg_path: knowledge/KNOWLEDGE_GRAPH.md   # this is the default; set only to override
---
```

## Notes

- **Interpreter portability.** `hooks.json` invokes
  `python3 <script> || python <script>`. Stock macOS/most Linux have only
  `python3`; stock Windows installs usually have only `python` — and both `||`
  forms work in POSIX `sh` *and* `cmd.exe` (a `command -v` probe would not).
  The usual objection to `a || b` chains — the script failing under `python3`
  and running twice — cannot apply here: `kg_post_edit_check.py` is fail-safe
  by contract (it swallows every error and always exits 0), so the fallback
  only fires when the `python3` interpreter itself is missing.
- Hooks load at session start — restart Claude Code after changing `hooks.json`.
- Test a hook directly: pipe a JSON event to `python hooks/kg_post_edit_check.py` (see the plugin's
  build history for example events). Malformed input → no output, exit 0 (by design).
- This is a *nudge*, not enforcement. The comprehensive-update contract (`SCHEMA.md` §6) is the
  author's responsibility; a hook can only notice the KG file went untouched, not whether the nodes
  are complete.

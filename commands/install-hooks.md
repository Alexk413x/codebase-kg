---
description: Install the codebase-kg pre-push staleness check into the current repo — an advisory hook that reports when a push adds source no node covers, or deletes source the graph still anchors on. Vendors a stdlib-only checker into the repo's hooks (portable for all clones/CI). Config-free — it reads root from the committed graph. Non-destructive to existing hooks.
argument-hint: "[repo path | empty = current repo]"
---

# /codebase-kg:install-hooks

Wire the pre-push staleness check into a repo. It **never blocks** — it compares the commits you are
pushing against the committed graph and reports two things: new source files no node covers, and
deleted files the graph still anchors on. See `git-hooks/README.md` for the design.

## Steps

### 1. Config — none needed (it's in the committed graph)
The check reads `root` from the graph's own `meta` table and auto-discovers
`knowledge/code_graph.db`. There is **no config file to write** — the shared, committed graph is the
config. (Only create a gitignored `.claude/codebase-kg.local.md` if a particular clone needs to
override it — see the template.) If the repo has no graph yet, run `/codebase-kg:build` first.

### 2. Detect the repo's hooks setup
Run `git config core.hooksPath`:

- **Set** (e.g. `.githooks`) → that's the hooks dir.
- **Unset** → use `.githooks/` and run `git config core.hooksPath .githooks` (the shareable pattern;
  `.git/hooks/` is not committed, so other clones wouldn't get it).

### 3. Vendor the checker
Copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/kg_pre_push.py` into the hooks dir. It is **stdlib-only**
(sqlite3 included), so it runs for every clone and CI with no plugin install.

### 4. Wire the `pre-push`
- **No existing `pre-push`** → copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/pre-push` into the hooks dir;
  `chmod +x` it.
- **Existing `pre-push`** → **do not overwrite it.** Add these two lines near the top (**before
  anything that reads stdin** — git feeds the pushed refs there and the checker consumes them). If
  it already contains an older blocking KG-freshness check, replace that block with this call:
  ```sh
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
  "$PY" "$(dirname "$0")/kg_pre_push.py" || true
  ```
  The `|| true` matters: this check is advisory and must not fail a push even if it errors.

### 5. Verify + explain
- Confirm the hook is executable and `core.hooksPath` resolves.
- Tell the user it is **advisory** and never blocks, so there is no `--no-verify` to remember. It
  answers "does this push move the code away from the map?" The fix is `/codebase-kg:refresh`;
  deeper drift is `/codebase-kg:validate`.

## Posture

Non-destructive: never overwrite an existing hook — integrate a call into it. Advisory, always
exit 0 (this replaced an earlier blocking, date-based gate — see `docs/DESIGN.md`). Everything is
committed in the repo (vendored checker + `.githooks/`), so all clones and CI behave the same.

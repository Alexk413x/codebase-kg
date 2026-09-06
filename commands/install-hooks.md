---
description: Install the codebase-kg staleness checks into the current repo — advisory pre-commit and pre-push hooks that report when a change adds source no node covers, or deletes source the graph still anchors on. Vendors a stdlib-only checker into the repo's hooks (portable for all clones/CI). Config-free — it reads root from the committed graph. Non-destructive to existing hooks.
argument-hint: "[repo path | empty = current repo]"
---

# /codebase-kg:install-hooks

Wire the staleness checks into a repo. They **never block** — they compare a change set against the
committed graph and report two things: new source files no node covers, and deleted files the graph
still anchors on. See `git-hooks/README.md` for the design.

Two hooks, because they answer at different moments:

- **`pre-commit`** checks what is STAGED. This is the one that catches things, because the commit
  needing the graph update is still in front of you. Skipped with `SKIP_KG=1`.
- **`pre-push`** checks the commits being pushed. It compares against the upstream branch, so once
  you have pushed it has nothing left to compare and reports nothing — which is exactly when someone
  thinks to look. Keep it, but do not rely on it alone.

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

### 4. Wire the `pre-commit`
- **No existing `pre-commit`** → copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/pre-commit` into the hooks
  dir; `chmod +x` it. Copy `kg_pre_commit.py` beside `kg_pre_push.py`; it imports the coverage rule
  from it rather than repeating it.
- **Existing `pre-commit`** → **do not overwrite it.** Add these lines near the top:
  ```sh
  [ -n "$SKIP_KG" ] || {
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
    "$PY" "$(dirname "$0")/kg_pre_commit.py" || true
  }
  ```

### 5. Wire the `pre-push`
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

### 6. Verify + explain
- Confirm both hooks are executable and `core.hooksPath` resolves.
- Tell the user they are **advisory** and never block. They answer "does this change move the code
  away from the map?" — `pre-commit` over what is staged, `pre-push` over what is being pushed.
  `SKIP_KG=1` silences the commit-time one when a change deliberately outruns the graph. The fix is `/codebase-kg:refresh`;
  deeper drift is `/codebase-kg:validate`.

## Posture

Non-destructive: never overwrite an existing hook — integrate a call into it. Advisory, always
exit 0 (this replaced an earlier blocking, date-based gate — see `docs/DESIGN.md`). Everything is
committed in the repo (vendored checker + `.githooks/`), so all clones and CI behave the same.

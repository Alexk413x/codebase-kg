---
description: Wire codebase-kg into the current repo in one pass — advisory pre-commit and pre-push staleness checks, and the textconv driver that makes the committed graph readable in git diffs. Idempotent, non-destructive to existing hooks, and safe to re-run any time to repair or update the wiring.
argument-hint: "[repo path | empty = current repo]"
---

# /codebase-kg:setup

One command for everything a repo needs. Run it after `/codebase-kg:build`, and again whenever the
wiring needs repairing — every step below is idempotent and none of them overwrite work that is
already there.

This replaces the old `install-hooks` and `setup-diff` commands.

## What is already handled without this command

The **search gate** and the **post-edit nudge** ship inside the plugin (`hooks/hooks.json`) and need
no per-repo install. They activate on their own in any repo that has a `knowledge/code_graph.db`,
and stay silent in any repo that does not. Mention this so the user knows the behavior is already
live; do not try to install it.

## Steps

### 1. Precondition — the repo has a graph
`knowledge/code_graph.db` must exist. If it does not, stop and tell the user to run
`/codebase-kg:build` first; every step here reads its `meta` table for `root`, so there is nothing to
wire without it.

### 2. Config — none needed (it's in the committed graph)
The checks read `root` from the graph's own `meta` table and auto-discover
`knowledge/code_graph.db`. There is **no config file to write** — the shared, committed graph is the
config. Only create a gitignored `.claude/codebase-kg.local.md` if a particular clone needs to
override it (see `templates/codebase-kg.local.md.example`).

### 3. Detect the repo's hooks setup
Run `git config core.hooksPath`:

- **Set** (e.g. `.githooks`) → that's the hooks dir.
- **Unset** → use `.githooks/` and run `git config core.hooksPath .githooks` (the shareable pattern;
  `.git/hooks/` is not committed, so other clones wouldn't get it).

### 4. Vendor the checker
Copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/kg_pre_push.py` into the hooks dir. It is **stdlib-only**
(sqlite3 included), so it runs for every clone and CI with no plugin install.

### 5. Wire the `pre-commit`
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

### 6. Wire the `pre-push`
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

### 7. Mark the graph in `.gitattributes`
The repo needs the `diff=codegraph` attribute. `/codebase-kg:build` normally writes it; add it if it
is missing. This line is **committed**, so every clone gets the wiring:

```
*.db binary diff=codegraph
code_graph.db binary diff=codegraph
```

Verify with `git check-attr diff -- knowledge/code_graph.db` — expect `diff: codegraph`.

### 8. Register the textconv driver
Without it, `git diff` says `Binary files a/knowledge/code_graph.db and b/knowledge/code_graph.db
differ` and a reviewer has to take the commit message on faith. With it, git diffs the graph's JSON
export, so a change shows up as which nodes, anchors and edges moved. Nothing about what is
committed changes — this only affects how git *displays* the file, per clone.

```sh
git config diff.codegraph.textconv "uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp codebase-kg-export"
git config diff.codegraph.binary true
git config diff.codegraph.cachetextconv true
```

`diff.codegraph.binary true` tells git the source really is binary, so it uses textconv for display
but never tries to generate a patch that could be applied back. `cachetextconv` caches converted
output per blob, so re-reviewing history does not re-export every time.

If the repo already has the package on its interpreter, prefer the faster form — `uvx` re-resolves
the environment on each invocation, which is noticeable across a long `git log -p`:

```sh
git config diff.codegraph.textconv "python -m codebase_kg.export"
```

### 9. Verify + explain
- Confirm both git hooks are executable and `core.hooksPath` resolves.
- Confirm `git diff HEAD~1 -- knowledge/code_graph.db` prints JSON with `+`/`-` lines rather than
  "Binary files differ". (If the graph has only one commit, say so instead of inventing a check.)
- Tell the user what is now live:
  - **Search gate** (from the plugin, no install): the first `Grep`/`Glob` or shell `grep`/`rg`/
    `find -name` of a session is denied once with the instruction to query the graph first, then it
    stands down for that session. Any codebase-kg MCP call stands it down too.
  - **Post-edit nudge** (from the plugin, no install): advisory, points at `/codebase-kg:refresh`.
  - **Git hooks** (installed here): advisory, never blocking. They answer "does this change move the
    code away from the map?" — `pre-commit` over what is staged, `pre-push` over what is being
    pushed. `SKIP_KG=1` silences the commit-time one, and the search gate, when a change
    deliberately outruns the graph.
  - The fix for drift is `/codebase-kg:refresh`; deeper drift is `/codebase-kg:validate`.

## What this does not solve

Merge conflicts on the graph. Two branches that both refresh it still conflict with no textual
merge. The resolution:

```sh
# During the conflict, git keeps both sides staged: :2 is ours, :3 is theirs.
git show :2:knowledge/code_graph.db > ours.db
git show :3:knowledge/code_graph.db > theirs.db
python -m codebase_kg.export ours.db   -o ours.json
python -m codebase_kg.export theirs.db -o theirs.json
# merge the JSON by hand, then rebuild and stage:
python -m codebase_kg.build merged.json -o knowledge/code_graph.db
git add knowledge/code_graph.db
```

That works because the build is deterministic — the same JSON always produces the same bytes — so a
rebuild after merging is reproducible rather than a third distinct artifact. See `docs/REVIEW.md`.

## Posture

Non-destructive: never overwrite an existing hook — integrate a call into it. Advisory, always
exit 0 (this replaced an earlier blocking, date-based gate — see `docs/DESIGN.md`). The hooks and
`.gitattributes` are committed in the repo, so all clones and CI behave the same; the textconv
driver is local git config, so a clone that skips setup just sees the old binary behavior.

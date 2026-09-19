---
description: Wire codebase-kg into the current repo in one pass — advisory pre-commit and pre-push staleness checks, the textconv driver that makes the committed graph readable in git diffs, and a committed install.sh so every other clone gets the same wiring from one command. Idempotent, non-destructive to existing hooks, and safe to re-run any time to repair or update the wiring.
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
- **Unset** → use `.githooks/`. Do **not** run `git config core.hooksPath` yourself here; step 9 runs
  the committed installer, which sets it in this clone the same way it will in every other one. One
  implementation, exercised by the person who wired the repo. (`.git/hooks/` is not committed, so
  other clones would not get it either way.)

### 4. Vendor the checkers and the installer
Copy **`${CLAUDE_PLUGIN_ROOT}/git-hooks/kg_pre_push.py` and
`${CLAUDE_PLUGIN_ROOT}/git-hooks/kg_pre_commit.py`** into the hooks dir. They are **stdlib-only**
(sqlite3 included), so they run for every clone and CI with no plugin install.

Copy both regardless of which hooks the repo already has. `kg_pre_commit.py` imports the coverage
and digest rules from `kg_pre_push.py` beside it rather than repeating them, so the two must land
together — and step 5 wires a call to it in either branch. An earlier version of this command copied
it only in the fresh-repo branch, so a repo with an existing `pre-commit` got a hook line pointing at
a file that was never installed; `|| true` swallowed the error and the check silently never ran while
step 10 reported it as live.

**Copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/install.sh` into the hooks dir too, and stamp its version
pin.** This is the file that makes every *other* clone work. `core.hooksPath` and the
`diff.codegraph.*` settings live in `.git/config`, which git never clones — so without a committed
installer, this command wires the one shell it runs in and every other checkout of the repo gets
inert hooks and "Binary files differ" forever, with nothing anywhere reporting it.

Stamp the pin after copying:

1. Read `version` from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`.
2. Confirm the matching tag is actually published — an unreleased version pins the installer at a
   URL that resolves for nobody:
   ```sh
   git ls-remote --tags https://github.com/Alexk413x/codebase-kg.git "codebase-kg--v<version>"
   ```
   If that prints nothing, use the newest tag it *does* list and say which version you pinned and
   why.
3. Rewrite the one line in the copied `install.sh`:
   ```sh
   KG_VERSION="${KG_VERSION:-<version>}"
   ```

The stamp is a starting value, not a live link. `install.sh` is committed, so the pin only moves when
someone re-runs this command and commits the change — which is what a pin is for. Anyone can override
per-clone with `KG_VERSION=` or `KG_TEXTCONV=` without editing the committed file.

**Tell the user if the plugin repo is private.** `install.sh` defaults to
`uvx --from git+https://github.com/Alexk413x/codebase-kg.git@…`, which only resolves for someone with
read access. On a private repo a teammate without access gets an auth prompt during onboarding, and a
teammate with access but no credential helper gets one too. Say so, and point at the `KG_TEXTCONV`
override for anyone who has the package locally.

### 5. Wire the `pre-commit`
- **No existing `pre-commit`** → copy `${CLAUDE_PLUGIN_ROOT}/git-hooks/pre-commit` into the hooks
  dir; `chmod +x` it.
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
  if command -v python3 >/dev/null 2>&1; then PY=python3
  elif command -v python >/dev/null 2>&1; then PY=python
  else PY=""; fi
  if [ -n "$PY" ] && [ -f "$(dirname "$0")/kg_pre_push.py" ]; then
    "$PY" "$(dirname "$0")/kg_pre_push.py" || exit $?
  fi
  ```
  **Do not append `|| true` here.** The checker returns non-zero for exactly one thing — a staleness
  backlog this push did not create — and catches its own errors so a bug in it can never fail a push.
  Swallowing the status leaves the reporting and removes the gate. The interpreter and file guards
  are what `|| true` used to cover: a `127` from a missing `python` must not block a push in a repo
  that cannot run the check at all.

  If the user wants the reporting without the gate, keep `|| true` — the repo-wide total still
  prints. Say so rather than deciding for them.

### 7. Mark the graph in `.gitattributes`
**Check what the repo already says before writing anything:**

```sh
git check-attr diff -- knowledge/code_graph.db
```

- **`diff: codegraph`** → already correct. Change nothing and move to step 8.
- **`diff: <something else>` or `unspecified`** → add the attribute.

The line is **committed**, so every clone gets the wiring. Which line to add depends on whether this
repo has other `.db` files:

- **The code graph is the only `.db`** → `*.db binary diff=codegraph` plus
  `code_graph.db binary diff=codegraph`, so a `graph_path` override still matches.
- **The repo has other graphs** (a driver corpus, a cartographer map, an app database) → add **only**
  the specific path: `knowledge/code_graph.db binary diff=codegraph`. A blanket `*.db` would capture
  them too, and git applies the LAST matching pattern — so a `*.db binary diff=codegraph` written
  underneath an existing `*_driver_graph.db binary diff=drivergraph` silently reroutes that graph
  through the wrong exporter. That is not a diff, it is an error on every `git show`.

Re-run `git check-attr` afterwards and confirm it reports `codegraph`. Never widen an attribute that
already resolves correctly — a repo that routes several graph types has done this deliberately.

### 8. Fix the committed exec bit on the hooks

A hook committed as mode `100644` — the normal outcome of writing it from Windows — is **skipped by
git on macOS and Linux with no message at all**. The check never runs, and nothing reports that it
did not. Check what is in the index, not what is on disk:

```sh
git ls-files -s -- .githooks/pre-commit .githooks/pre-push .githooks/kg_pre_commit.py .githooks/kg_pre_push.py
```

Any line starting `100644` needs correcting. `chmod +x` alone does not fix it on Windows, where
`core.filemode` is false and git ignores the on-disk bit entirely — the index is the only thing that
travels:

```sh
git update-index --chmod=+x .githooks/pre-commit .githooks/pre-push .githooks/kg_pre_commit.py .githooks/kg_pre_push.py
```

That stages a mode change. Tell the user it needs committing along with the rest of the wiring;
uncommitted, it fixes nothing for anyone else.

### 9. Run the committed installer

Everything that is per-clone — `core.hooksPath` and the three `diff.codegraph.*` settings — lives in
`.git/config`, which git never clones. Rather than setting it here, run the installer you vendored in
step 4, from the repo root:

```sh
sh .githooks/install.sh
```

It sets `core.hooksPath`, `chmod +x`s the four hook files, registers the textconv driver, and then
**probes it against the real graph** and reports if it did not render JSON. It is idempotent, so
re-running is free.

Do not re-implement its `git config` calls here. The whole defect this fixes was setup wiring its own
shell and nothing else; the fix only holds if the shell setup wires is the same one a fresh clone
gets.

**The textconv value it writes is machine-independent, on purpose:**

```
uvx --quiet --from "git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v<version>#subdirectory=mcp" codebase-kg-export
```

- **Never `${CLAUDE_PLUGIN_ROOT}`.** That expands to a version-stamped local cache path
  (`…/.claude/plugins/cache/codebase-kg/codebase-kg/0.5.2/mcp`) and is written into `.git/config`
  verbatim. It resolves on exactly one machine, and breaks there on the next plugin update. Git
  reports nothing when a textconv command is missing — it just shows the binary fallback.
- **A tag, never a branch.** This runs on every diff of the graph; it must not change under the repo
  silently.
- **`--quiet` is not cosmetic.** Without it, uv prints resolution lines into the body of every diff.

If `uvx` is missing, the installer skips the textconv half with an explanation and still wires the
hooks. Report that outcome as a skip, not as success.

### 10. Verify + explain

**Verify the wiring the way a teammate will meet it — in a clone, not in this shell.** Setup's old
step 9 checked `git diff` in the shell that had just run `git config`, which is a test of the value
it set one line earlier and passes no matter how machine-local that value is. It reported the
textconv driver as live in a repo where every other clone saw "Binary files differ".

Commit the wiring first (`.githooks/`, the mode changes, `.gitattributes`), then:

```sh
# Diff a range that actually touched the graph. HEAD~1..HEAD is usually the
# wiring commit, which does not touch it, and an empty diff proves nothing.
NEW=$(git log -1 --format=%H -- knowledge/code_graph.db)
OLD=$(git log -2 --format=%H -- knowledge/code_graph.db | tail -1)

TMP=$(mktemp -d)
git clone --shared --no-checkout . "$TMP/kgverify"
# .gitattributes is what routes the graph to diff=codegraph. Omit it and
# check-attr resolves to nothing, so the diff falls back to "Binary files
# differ" no matter how correct the wiring is — a confident false failure.
git -C "$TMP/kgverify" sparse-checkout set --no-cone .githooks knowledge .gitattributes
git -C "$TMP/kgverify" checkout
# Run it from inside the clone. install.sh anchors on its own location, but
# staying out here hides a regression if that ever breaks again.
(cd "$TMP/kgverify" && sh .githooks/install.sh)
git -C "$TMP/kgverify" check-attr diff -- knowledge/code_graph.db
git -C "$TMP/kgverify" diff "$OLD" "$NEW" -- knowledge/code_graph.db | head -20
rm -rf "$TMP"
```

That clone has its own empty `.git/config`, so it can only pass if the committed installer is what
made it pass. Confirm `check-attr` reports `codegraph` and the diff prints JSON with `+`/`-` lines
rather than "Binary files differ". If the graph has only one commit, `OLD` and `NEW` are the same SHA and
there is nothing to diff — say so instead of inventing a check.

Also confirm, in this repo:

- `git ls-files -s` reports `100755` for all four hook files (step 8).
- `git check-attr diff -- knowledge/code_graph.db` reports `codegraph` (step 7).
- `git config --get core.hooksPath` resolves to the hooks dir.

Then tell the user what is now live:

- **Search gate** (from the plugin, no install): the first `Grep`/`Glob` or shell `grep`/`rg`/
  `find -name` of a session is denied once with the instruction to query the graph first, then it
  stands down for that session. Any codebase-kg MCP call stands it down too.
- **Post-edit nudge** (from the plugin, no install): advisory, points at `/codebase-kg:refresh`.
- **Unwired-clone notice** (from the plugin, no install): at session start, a clone of this repo with
  no `core.hooksPath` or no `diff.codegraph.textconv` gets one line naming `sh .githooks/install.sh`.
  It only prints — it never writes git config, because git leaves `.git/config` out of a clone
  precisely so that cloning cannot cause code to run.
- **Git hooks** (installed here): they answer "does this change move the code away from the map?" —
  `pre-commit` over what is staged, `pre-push` over what is being pushed. Both also report the
  repo-wide staleness total, which no change set can see: a file that drifts and is never re-derived
  is named once and then never again. `pre-commit` is advisory and adds one line for that total.
  `pre-push` blocks on one thing only — mapped files that have drifted and that this push does not
  touch. That is zero in a repo kept current; release it with `KG_STALE_ACK=<count>` (the count is
  in the message and the ack expires when it moves), `SKIP_KG=1`, or `git push --no-verify`.
  `SKIP_KG=1` silences either hook, and the search gate, when a change deliberately outruns the
  graph.
- **One command per clone**: everyone else who clones this repo runs `sh .githooks/install.sh` once.
  It needs plain git, POSIX sh and `uv` — no Claude Code and no plugin install.
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
exit 0 (this replaced an earlier blocking, date-based gate — see `docs/DESIGN.md`). The hooks,
`install.sh` and `.gitattributes` are committed in the repo, so all clones and CI behave the same;
`core.hooksPath` and the textconv driver are local git config, which git never clones, so each clone
runs `sh .githooks/install.sh` once. A clone that skips it sees inert hooks and the old binary
behavior — the session-start notice is what makes that visible instead of silent.

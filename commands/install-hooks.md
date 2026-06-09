---
description: Install the codebase-kg pre-push gate into the current repo — a blocking hook that rejects a push when source changed but the KG wasn't updated. Vendors a stdlib-only checker into the repo's hooks (portable for all clones/CI). Writes per-repo config. Non-destructive to existing hooks.
argument-hint: "[repo path | empty = current repo]"
---

# /codebase-kg:install-hooks

Wire the pre-push KG freshness gate into a repo. The gate **blocks** a push when tracked source
under the KG's `root` changed but `KNOWLEDGE_GRAPH.md` wasn't updated to match (overridable with
`git push --no-verify`). See `git-hooks/README.md` for the design.

## Steps

### 1. Per-repo config
If `.claude/codebase-kg.local.md` is missing, create it from `templates/codebase-kg.local.md.example`
with `codebase`, `root` (the source dir — only changes under here trigger the gate), `kg_path`, and
`counterpart` (the peer KG, for parity). Confirm the inferred values with the user once.

### 2. Detect the repo's hooks setup
Run `git config core.hooksPath`:
- **Set** (e.g. `.githooks`) → that's the hooks dir.
- **Unset** → use `.githooks/` and run `git config core.hooksPath .githooks` (the shareable pattern;
  `.git/hooks/` is not committed, so other clones wouldn't get the gate).

### 3. Vendor the checker
Copy `git-hooks/kg_pre_push.py` (from the plugin) into the hooks dir. It is **stdlib-only**, so it
runs for every clone/CI with no plugin install.

### 4. Wire the `pre-push`
- **No existing `pre-push`** → copy `git-hooks/pre-push` into the hooks dir; `chmod +x` it.
- **Existing `pre-push`** (e.g. a repo that already runs tests/lint on push) → **do not overwrite
  it.** Add these two lines near the top (after `set -e` if present), and if it already has its own
  ad-hoc KG-freshness check, replace that block with this call:
  ```sh
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
  "$PY" "$(dirname "$0")/kg_pre_push.py" || exit 1
  ```

### 5. Verify + explain
- Confirm the hook is executable and `core.hooksPath` resolves.
- Tell the user the gate is **freshness-only** (deterministic): it ensures the KG ships with the
  code. The semantic update is `/codebase-kg:refresh` (updates changed nodes + `updated` dates +
  reconciles parity vs the peer KG); deeper drift is `/codebase-kg:validate`. Override: `--no-verify`.

## Posture

Non-destructive: never overwrite an existing hook — integrate a call into it. The gate blocks (the
user chose enforcement) but always has the `--no-verify` escape hatch. Everything is committed in the
repo (vendored checker + `.githooks/`), so all clones and CI get the same gate.

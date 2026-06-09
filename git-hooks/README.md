# git-hooks/ — pre-push KG freshness gate

A **blocking, deterministic, vendorable** git pre-push hook: it rejects a push when tracked
**source** files under the KG's `root` changed in the about-to-push commits but
`KNOWLEDGE_GRAPH.md` wasn't updated to match (not in the changeset, or its header `refreshed:`
isn't today). Override with `git push --no-verify`.

This is the **enforcement half**. The **semantic half** — actually updating the changed nodes,
bumping their per-node `updated` dates, reconciling `parity`/`counterpart` against the peer KG, and
running `kg_validate` — is the agent's job via `/codebase-kg:refresh`. The gate just guarantees you
don't push code whose KG is out of sync; it points you at the refresh.

| File | Role |
|---|---|
| `kg_pre_push.py` | The check — **stdlib only, no codebase-kg dependency**, so it vendors into any repo. Reads `root` from the **committed KG header** (auto-discovers the KG; an optional, gitignored `.claude/codebase-kg.local.md` may override). No committed config file required. |
| `pre-push` | Thin `sh` wrapper that runs `kg_pre_push.py` next to it. |

## Why vendored (copied into the repo) and not referenced from the plugin

A git hook runs for **every clone** of the repo — other devs, CI — where the plugin isn't installed
and `${CLAUDE_PLUGIN_ROOT}` isn't set. So the check is pure-stdlib and gets **copied into the
repo's hooks dir**, with no path back to the plugin. It works anywhere Python 3 + git exist.

## Install

Use `/codebase-kg:install-hooks` (agent-guided — handles the cases below), or by hand:

**Fresh repo (no existing hooks):**
```sh
mkdir -p .githooks
cp <plugin>/git-hooks/kg_pre_push.py .githooks/
cp <plugin>/git-hooks/pre-push       .githooks/
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

**Repo that already has a pre-push hook** (don't overwrite it): copy `kg_pre_push.py` into the
hooks dir and add this near the top of the existing `pre-push`:
```sh
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
"$PY" "$(dirname "$0")/kg_pre_push.py" || exit 1
```

No config file is needed — the gate reads `root` from the committed KG header and auto-discovers the
KG at `knowledge/KNOWLEDGE_GRAPH.md`. (A gitignored `.claude/codebase-kg.local.md` can override the
header for one clone; see `templates/codebase-kg.local.md.example`.)

## What triggers a block

- A tracked file under `root` changed in the push range **and** it's a real source file (not the KG,
  not a doc/config like `.md`/`.json`/`.toml`, not in `build/`, `node_modules/`, `.git/`, …), **and**
- `KNOWLEDGE_GRAPH.md` is **not** in the same push range, **or** its header `refreshed:` (or legacy
  `last refreshed`) isn't today's date.

No source change → never blocks. The freshness check is intentionally cheap and dependency-free;
deeper structural drift (dangling edges, ungreppable anchors, parity reciprocity) is the agent's
`/codebase-kg:validate` + `/codebase-kg:refresh`.

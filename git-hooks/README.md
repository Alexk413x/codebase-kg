# git-hooks/ — pre-push code-graph staleness check

An **advisory, vendorable** git pre-push hook. It compares the commits you are about to push against
the committed `knowledge/code_graph.db` and reports two things:

- **new source files no node covers** — code nobody mapped;
- **deleted source files the graph still anchors on** — pointers into code that is gone.

It **never blocks**. Exit status is always 0, so there is no `--no-verify` to remember.

| File | Role |
|---|---|
| `kg_pre_push.py` | The check — **stdlib only** (sqlite3 included), **no codebase-kg dependency**, so it vendors into any repo. Reads `root` from the committed graph's `meta` table (auto-discovers the graph; an optional, gitignored `.claude/codebase-kg.local.md` may override). No committed config file required. |
| `pre-push` | Thin `sh` wrapper that runs `kg_pre_push.py` next to it. |

## Why it doesn't block any more

The previous version blocked a push when source changed and the graph's `refreshed:` header wasn't
today's date. That was wrong twice over:

1. It contradicted the plugin's own "advisory, never blocking" principle — and hard gates get worked
   around, not obeyed.
2. **A date cannot measure freshness.** It proves someone edited the file, not that the nodes match
   the code. In practice a real graph sat at `refreshed: 2026-07-12` with three nodes stale from a
   later commit — under a gate designed to prevent exactly that.

The current check asks questions with real answers, scoped to the changeset you're pushing. It
points at `/codebase-kg:refresh` and gets out of the way.

## Why vendored (copied into the repo) and not referenced from the plugin

A git hook runs for **every clone** of the repo — other devs, CI — where the plugin isn't installed
and `${CLAUDE_PLUGIN_ROOT}` isn't set. So the check is pure-stdlib and gets **copied into the repo's
hooks dir**, with no path back to the plugin. It works anywhere Python 3 + git exist.

## Install

Use `/codebase-kg:install-hooks` (agent-guided — handles the cases below), or by hand:

**Fresh repo (no existing hooks):**
```sh
mkdir -p .githooks
cp "$PLUGIN/git-hooks/kg_pre_push.py" .githooks/
cp "$PLUGIN/git-hooks/pre-push" .githooks/
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

**Repo that already has a `pre-push`:** don't overwrite it. Copy `kg_pre_push.py` in and add two
lines near the top — **before anything that reads stdin**, since git feeds the pushed refs there and
this check consumes them:

```sh
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
"$PY" "$(dirname "$0")/kg_pre_push.py" || true
```

The `|| true` matters: an advisory check must never fail a push, even if it errors.

## What counts as source

Same rules as the in-session hook: files under the graph's `root`, excluding docs/config extensions
(`.md`, `.json`, `.yaml`, …) and ignored directories (`.git`, `node_modules`, `build`, `Pods`,
`DerivedData`, …). The graph file itself never triggers it.

A modification to an already-mapped file is deliberately *not* reported — that would fire on every
push and mean nothing. Description drift is what `kg_validate` and `kg-audit` are for.

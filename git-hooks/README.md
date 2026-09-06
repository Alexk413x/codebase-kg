# git-hooks/ — code-graph staleness checks

**Advisory, vendorable** git hooks. They compare a change set against the committed
`knowledge/code_graph.db` and report three things:

- **source files no node covers** — code nobody mapped, whether this change added it or only
  touched it;
- **deleted source files the graph still anchors on** — pointers into code that is gone;
- **mapped files whose contents no longer match the digest recorded when the graph was built**
  (SCHEMA.md §6.3) — the anchor still resolves, so nothing else notices, but the description may no
  longer fit.

They **never block**. Exit status is always 0.

### Why the third one exists

The first two versions of this check read only git's `A` and `D` status letters. A real change set
is mostly `M`, so the check was silent through exactly the drift that accumulates: on one repo it
would have named the 27 new files and said nothing about the 47 modified ones — two thirds of a
graph that had fallen 48 commits behind. The `source` table of digests was written on every build
and read by neither hook.

Digests are read out of git, never off disk: the index (`:path`) at commit time, the pushed tips at
push time. A push of a branch that is not checked out would otherwise be compared against whatever
happens to be in the working tree. A file with no recorded baseline is not reported — absent
evidence reads as "no baseline", never as "unchanged".

## Two hooks, because they answer at different moments

`pre-commit` checks what is **staged**. This is the one that catches things: the commit that needs
the graph update is still in front of you, and staged-against-HEAD is a comparison that is always
available.

`pre-push` checks the commits being **pushed**, against the upstream branch. That range is empty
once you have pushed — so the check goes quiet at precisely the moment someone thinks to look at it.
It is still worth having for the commits it does see, but it is not the one to rely on.

Neither runs `/codebase-kg:refresh`, and neither can. Refresh maps changed files to nodes, hands a
JSON diff to a person to read, and decides what to add, edit or remove. A shell hook has no way to
make those calls, and one that wrote its own guess into the graph would be manufacturing knowledge
rather than recording it. The hooks say the graph needs attention; a person or an agent refreshes it.

`SKIP_KG=1` silences the commit-time check when a change deliberately outruns the graph.

| File | Role |
|---|---|
| `kg_pre_push.py` | The check — **stdlib only** (sqlite3 included), **no codebase-kg dependency**, so it vendors into any repo. Reads `root` from the committed graph's `meta` table (auto-discovers the graph; an optional, gitignored `.claude/codebase-kg.local.md` may override). No committed config file required. |
| `pre-push` | Thin `sh` wrapper that runs `kg_pre_push.py` next to it. |
| `kg_pre_commit.py` | The same check over the staged change set. Imports the coverage rule from `kg_pre_push.py` rather than repeating it, so there is one implementation to keep in step with `codebase_kg/coverage.py`. |
| `pre-commit` | Thin `sh` wrapper, honouring `SKIP_KG`. |

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

Use `/codebase-kg:setup` (agent-guided — handles the cases below), or by hand:

**Fresh repo (no existing hooks):**
```sh
mkdir -p .githooks
cp "$PLUGIN/git-hooks/kg_pre_push.py" .githooks/
cp "$PLUGIN/git-hooks/pre-push" .githooks/
cp "$PLUGIN/git-hooks/kg_pre_commit.py" .githooks/
cp "$PLUGIN/git-hooks/pre-commit" .githooks/
chmod +x .githooks/pre-push .githooks/pre-commit
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

A modification to an already-mapped file is reported only when its bytes no longer match the digest
recorded at build time. The status letter alone is not the signal — reporting every `M` would fire on
every push and mean nothing, which is why an earlier version reported none of them and went silent
through most of the drift instead. The digest is what makes the difference between "this file was
touched" and "this file is no longer what the description was written against". Judging whether the
description still fits is still `kg_validate` and `kg-audit`; this only says where to look.

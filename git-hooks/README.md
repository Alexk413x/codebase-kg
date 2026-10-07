# git-hooks/ — code-graph staleness checks

**Vendorable** git hooks. They compare a change set against the committed
`knowledge/code_graph.db` and report three things:

- **source files no node covers** — code nobody mapped, whether this change added it or only
  touched it;
- **deleted source files the graph still anchors on** — pointers into code that is gone;
- **mapped files whose contents no longer match the digest recorded when the graph was built**
  (SCHEMA.md §6.3) — the anchor still resolves, so nothing else notices, but the description may no
  longer fit.

All three are **advisory**, at both hooks. On top of them each hook also reports the **repo-wide**
staleness total, and `pre-push` blocks on it — see
[The one thing that blocks](#the-one-thing-that-blocks).

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

## The repo-wide total

Change-set scoping is right for per-commit noise and wrong for a standing gap. A file that drifts and is
never re-derived is reported once, in the commit that touched it, and never again. Miss it once and
it is invisible: one repo carried 47 stale files for months with every check above passing, because
nothing ever asked the standing question.

So both hooks also compare **every anchored file** against its baseline, not just the ones in the
change set. `pre-commit` prints that as one line, and one line only — its scoping is correct and
must not become noisy. `pre-push` prints the total, the nodes it puts in doubt, and every stale
file.

`kg_stats` reports the same numbers, from the same helper (`codebase_kg/staleness.py`). One
comparison rule, four callers: the digest folds CRLF to LF before hashing, because the builder reads
the working tree while the hooks read the git blob, and a caller that re-derived the comparison for
itself got 113 stale files where the truth was 47.

## The one thing that blocks

`pre-push` exits non-zero when any **mapped file is stale**: its contents no longer match the digest
recorded when the graph was built. The gate covers every stale file, including the files this push
touches. Nothing else blocks — not the change-set findings, not an error inside the check itself,
and never `pre-commit`.

A commit is provisional; a push is publication. The remote records the code, and a graph that lags
the code at that point stays wrong for everyone who reads it. To push, refresh the graph first, or
acknowledge the drift explicitly. In a repo that is kept current no file is stale and this hook is
silent, so there is no habit of bypassing it to develop.

Agents that run `git push` through Claude Code meet a `PreToolUse` hook first
(`hooks/kg_push_gate.py`, see [hooks/README.md](../hooks/README.md#the-push-gate-pretooluse)). It
denies the push on the same rule and tells the agent to refresh, commit and push again. It does not
accept `--no-verify`.

### The hook tries to refresh the graph first

When the gate would block, `pre-push` first runs `claude -p "/codebase-kg:refresh"` in the repo
root. It prints a line before it starts. The run can take a few minutes and costs **one headless
model run per stale push**.

The hook runs the refresh only when all of these hold:

- `claude` is on `PATH`.
- `KG_AUTO_REFRESH` is not `0`, and `KG_REFRESHING` is not set.
- A pushed ref points at `HEAD`, so the refreshed graph can be committed on top of the push.
- The graph file has no uncommitted changes.

The run is unattended, so it gets less than the refresh skill's own `allowed-tools`:

- The graph's MCP tools. The write tools validate each change and write only the graph file.
- `Read(./**)`, `Grep` and `Glob`, and the read-only `git` commands the skill runs.
- No `Write`, no `Edit` and no CLI runner, so text in the repo cannot steer the run into writing or
  running anything else. The prompt tells the skill to use the write tools, not the export and
  build path.
- A minimal environment: the variables `claude` needs to start, sign in and reach the API, plus
  `KG_REFRESHING=1`, so a refresh cannot trigger another one. The `GIT_*` variables git sets for
  the hook do not reach the run.

The hook stops the run after 900 seconds. Review the refresh commit with `git show` before you
push again.

If the run exits 0, the graph file changed, and no mapped file is stale against `HEAD`, the hook
commits the graph as "Refresh the code graph". The push still fails with exit status 1. A pre-push
hook cannot add a commit to the push in progress, so run `git push` again.

Every other outcome prints the reason, leaves any graph change uncommitted, and falls through to
the normal block: `claude` fails or times out, a file stays stale, or the hook raises an error.

Three ways past the block, all explicit:

| | |
|---|---|
| `KG_STALE_ACK=<n> git push …` | Accept this exact set of stale files. `<n>` is the total stale count the message prints. It names the number on purpose — the ack stops matching the moment the count moves, so it cannot be set once in a shell profile and forgotten. |
| `SKIP_KG=1 git push …` | Skip the check, as at commit time. |
| `KG_AUTO_REFRESH=0 git push …` | Keep the check and the block. Skip only the automatic refresh. |
| `git push --no-verify` | Skip every hook. |

An unexpected error inside the check is **not** a block: it reports itself on stderr and exits 0. A
staleness check that fails pushes when it has a bug is worse than no staleness check. The `pre-push`
wrapper no longer swallows the exit status with `|| true` — that is what makes the gate a gate — so
it also guards against a missing interpreter and a missing checker file.

Neither hook runs `/codebase-kg:refresh`, and neither can. Refresh maps changed files to nodes, hands a
JSON diff to a person to read, and decides what to add, edit or remove. A shell hook has no way to
make those calls, and one that wrote its own guess into the graph would be manufacturing knowledge
rather than recording it. The hooks say the graph needs attention; a person or an agent refreshes it.

`SKIP_KG=1` silences either hook when a change deliberately outruns the graph.

| File | Role |
|---|---|
| `kg_pre_push.py` | The check — **stdlib only** (sqlite3 included), **no codebase-kg dependency**, so it vendors into any repo. Reads `root` from the committed graph's `meta` table (auto-discovers the graph; an optional, gitignored `.claude/codebase-kg.local.md` may override). No committed config file required. |
| `pre-push` | Thin `sh` wrapper that runs `kg_pre_push.py` next to it and **propagates its exit status**, so the stale-file gate is a gate. Guards against a missing interpreter or checker rather than letting either block a push. |
| `kg_pre_commit.py` | The same check over the staged change set, plus the one-line repo-wide total. Imports the coverage and staleness rules from `kg_pre_push.py` rather than repeating them, so there is one implementation to keep in step with `codebase_kg/coverage.py` and `codebase_kg/staleness.py`. |
| `pre-commit` | Thin `sh` wrapper, honouring `SKIP_KG`. |
| `install.sh` | Per-clone wiring: `core.hooksPath`, the exec bits, and the three `diff.codegraph.*` settings. Vendored beside the checkers, because the settings it writes are the ones git never clones. |

## What the old gate got wrong

A much earlier version blocked a push when source changed and the graph's `refreshed:` header wasn't
today's date. That was wrong twice over:

1. It fired on every push that touched source, so it was worked around rather than obeyed.
2. **A date cannot measure freshness.** It proves someone edited the file, not that the nodes match
   the code. In practice a real graph sat at `refreshed: 2026-07-12` with three nodes stale from a
   later commit — under a gate designed to prevent exactly that.

Both faults are about *what* was gated, not about gating. The stale-file gate above measures a real
thing — digests, not a date — and is zero in a repo that is kept current, so there is nothing to
develop a habit of bypassing.

## Why vendored (copied into the repo) and not referenced from the plugin

A git hook runs for **every clone** of the repo — other devs, CI — where the plugin isn't installed
and `${CLAUDE_PLUGIN_ROOT}` isn't set. So the check is pure-stdlib and gets **copied into the repo's
hooks dir**, with no path back to the plugin. It works anywhere Python 3 + git exist.

## Install

### Per clone — the one command

Everything that makes these hooks run lives in `.git/config`, and **git never clones `.git/config`**.
A repo can have the checkers committed, the wrapper committed and `.gitattributes` committed, and
every fresh checkout still gets inert hooks and `Binary files differ` on the graph, with no error
anywhere saying so. That is why `install.sh` is vendored beside the checkers:

```sh
sh .githooks/install.sh
```

Plain git, POSIX sh, and `uv` for the diff half. No Claude Code, no plugin install. Idempotent —
re-running changes nothing that is already correct. It:

- sets `core.hooksPath` (and refuses to clobber one already pointing elsewhere);
- `chmod +x`es the four hook files, and names the ones committed as `100644` so someone can fix the
  index with `git update-index --chmod=+x` — on macOS and Linux git skips a non-executable hook with
  no message at all;
- runs the driver against the real graph **before** configuring it, and leaves it unset if it did
  not render JSON — a broken textconv does not fall back to "Binary files differ", it makes
  `git diff` fail outright, so an unverified driver is worse than none;
- registers `diff.codegraph.textconv`, `.binary` and `.cachetextconv` once it works.
  `.cachetextconv` is skipped when the clone has no git identity: its cache lives in a notes ref,
  which needs one, and without it git fails the whole diff rather than skipping the cache.

The textconv command it writes is a **tag-pinned** remote, so it means the same thing on every
machine:

```
uvx --quiet --from "git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v0.5.4#subdirectory=mcp" codebase-kg-export
```

`--quiet` is load-bearing: without it uv prints resolution lines into the body of every diff. The tag
is load-bearing too — this command runs on every diff of the graph and must not change under the repo
silently.

Overrides, all per-clone and none needing an edit to the committed file:

| Env | Use |
|---|---|
| `KG_VERSION` | pin a different published tag |
| `KG_SOURCE` | a different `uvx --from` source entirely |
| `KG_TEXTCONV` | replace uvx — e.g. `KG_TEXTCONV="python -m codebase_kg.export"` when the package is already installed, which is faster across a long `git log -p` |

If the plugin repo is private, `uvx` needs credentials for it. Someone with access and a credential
helper is fine; anyone else should use `KG_TEXTCONV`.

### Into a repo, the first time

Use `/codebase-kg:setup` (agent-guided — handles the cases below), or by hand:

**Fresh repo (no existing hooks):**
```sh
mkdir -p .githooks
cp "$PLUGIN/git-hooks/kg_pre_push.py"   .githooks/
cp "$PLUGIN/git-hooks/pre-push"         .githooks/
cp "$PLUGIN/git-hooks/kg_pre_commit.py" .githooks/
cp "$PLUGIN/git-hooks/pre-commit"       .githooks/
cp "$PLUGIN/git-hooks/install.sh"       .githooks/
git add .githooks
git update-index --chmod=+x .githooks/pre-push .githooks/pre-commit .githooks/kg_pre_push.py .githooks/kg_pre_commit.py
sh .githooks/install.sh
```

Commit `.githooks/` and the `.gitattributes` line. Then every other clone needs only the one command
above.

**Repo that already has a `pre-push`:** don't overwrite it. Copy `kg_pre_push.py` in and add two
lines near the top — **before anything that reads stdin**, since git feeds the pushed refs there and
this check consumes them:

```sh
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else PY=""; fi
if [ -n "$PY" ] && [ -f "$(dirname "$0")/kg_pre_push.py" ]; then
  "$PY" "$(dirname "$0")/kg_pre_push.py" || exit $?
fi
```

Don't append `|| true`. The checker returns non-zero only for unacknowledged stale files — it catches
its own errors and returns 0 — so swallowing the status leaves you with the reporting and none of the
gate. The interpreter and file guards are there because a `127` from a missing `python` would block
a push in a repo that cannot run the check at all.

Near the top is necessary but not sufficient. A shell script exits with the status of its **last**
command, so anything the wrapper runs after this — lint, another quality gate — overwrites a block
with its own `0` and the push goes through silently. That happened in a real repo. If work follows
the call, capture the status and re-raise it at the end instead of exiting inline:

```sh
kg_status=0
if [ -n "$PY" ] && [ -f "$(dirname "$0")/kg_pre_push.py" ]; then
  "$PY" "$(dirname "$0")/kg_pre_push.py" || kg_status=$?
fi

# ... the repo's own gates ...

exit $kg_status
```

If you want the reporting without the gate, keep `|| true`; the repo-wide total still prints.

## What counts as source

Same rules as the in-session hook: files under the graph's `root`, excluding docs/config extensions
(`.md`, `.json`, `.yaml`, …) and ignored directories (`.git`, `node_modules`, `build`, `Pods`,
`DerivedData`, …). The graph file itself never triggers it.

A modification to an already-mapped file is reported only when its bytes no longer match the digest
recorded at build time. The status letter alone is not the signal — reporting every `M` would fire on
every push and mean nothing, which is why an earlier version reported none of them and went silent
through most of the drift instead. The digest is what makes the difference between "this file was
touched" and "this file is no longer what the description was written against". Judging whether the
description still fits is still `kg_validate` and `audit`; this only says where to look.

# hooks/

Six Claude Code hook scripts. All ship with the plugin — there is **nothing to install per repo**.
The search gate, the push gate, the post-edit nudge and the unwired-clone notice no-op in a repo with
no `knowledge/code_graph.db`. The server start and the session end run in every session.

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config — PreToolUse on `Grep\|Glob`, on `Bash` and `PowerShell` calls that an `if` rule names as a search (`Bash(grep *)`, `PowerShell(Select-String *)`, one rule per search word), and on the codebase-kg MCP tools → `kg_search_gate.py` (`async: true`, on purpose: the hook only credits the query, so the query never waits for it); `Bash(git *)` calls → `kg_push_gate.py`; PostToolUse on `Edit\|Write` → `kg_post_edit_check.py`; SessionStart on `startup\|resume\|clear` → `kg_server_start.py`, then `kg_session_start.py`; SessionEnd → `kg_session_end.py`. Every hook command is `. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/<script>.cmd"`; see "How a hook starts" below. |
| `codex-hooks.json` | The same hooks for Codex, named by `hooks` in `.codex-plugin/plugin.json`: the search gate and push gate on `Bash`, the credit hook on codebase-kg MCP calls, the nudge on `apply_patch`, and the session notice. It has no `if`, `Grep`, `Glob` or `PowerShell` entry, because Codex ignores `if` and reports shell commands as `Bash`, so each script decides for itself whether a command is a search or a push. It has no server start hook: the `--start` option of `mcp/launch/kg_headers.py`, which the Codex config table runs as its headers helper, or the stdio shim starts the server. `commandWindows` runs `py -3` and names the script through `$env:PLUGIN_ROOT`, because Codex runs it in PowerShell, where `${CLAUDE_PLUGIN_ROOT}` is empty; `command` runs `python3`. |
| `copilot-hooks.json` | The hooks for GitHub Copilot CLI, named by `hooks` in `.github/plugin/plugin.json`: `preToolUse` and `postToolUse` run `kg_copilot.py` with `python`. |
| `kg_copilot.py` | The Copilot CLI adapter. Turns Copilot's hook payloads into the shape the shared search gate, push gate and nudge read, so those scripts stay unchanged. |
| `kg_search_gate.py` | The gate. Denies a search aimed at mapped code with the instruction to query the graph, and keeps doing it — a query buys credit, a located search is free, a repeat always passes. |
| `kg_push_gate.py` | The push gate. Denies an agent's `git push` while any mapped file is stale against `HEAD`. |
| `kg_post_edit_check.py` | The nudge. Two signals: the edited file isn't in the graph at all, or enough mapped files have changed since the graph was rebuilt. |
| `kg_server_start.py` | The server start, and the session register. Starts the shared HTTP server when nothing answers on the `server_port` setting, or an older build does, and registers the session. One line only when the port belongs to another program or the server does not come up. |
| `kg_session_end.py` | The session unregister, on `SessionEnd`. Deletes the session's registry file; prints nothing. |
| `kg_session_start.py` | The unwired-clone notice. One line when this clone has the committed checkers but no `core.hooksPath` or no `diff.codegraph.textconv`. It never wires a clone. In a wired clone it moves the release the diff driver pins to the installed plugin's, and with `--codex` it runs `_codex_config.py`. |
| `_codex_config.py` | Moves the codebase-kg paths in Codex's `config.toml` to the installed plugin's folder after an update. |
| `_config.py` | Reads `root`, `covers` and `exempt` from the committed graph's `meta` table (auto-discovers `knowledge/code_graph.db`); a gitignored `.claude/codebase-kg.local.md` may override. Decides what counts as a source file. Holds the helpers the hooks share: state files, the deny output, the `SKIP_KG` test. |
| `_shell_search.py` | The search gate's shell-command parsing: which clauses of a Bash or PowerShell command are searches, where each looks and for what. The gate decision stays in `kg_search_gate.py`. |
| `_shim_loader.py` | Loads `mcp/src/codebase_kg/shim.py` by path, for the hooks that start or register with the server. |

## How a hook starts

Claude Code runs a shell-form hook command with `sh -c` on macOS and Linux, in Git Bash on Windows,
and in PowerShell on Windows when Git Bash is not installed (it falls back to `powershell.exe`, 5.1,
when there is no `pwsh`). One command string has to work in all of them, so each hook is

```
. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/kg_search_gate.cmd"; exit $LASTEXITCODE
```

`.` sources the file in `sh` and runs it in PowerShell (PowerShell resolves the placeholder from
`${env:CLAUDE_PLUGIN_ROOT}`). The file is one text that both read. The first line, `:<<"::KG_CMD"`,
opens a here-document in `sh` that hides the batch lines, and is a label to `cmd.exe`, which runs the
batch lines when PowerShell starts the `.cmd`. Each half probes `py -3`, then `python3`, then `python`
by running it with `-c ""`, so the Microsoft Store `python3` stub is skipped, and then runs the script
once: a script that exits non-zero is not retried under another interpreter. The headers helper in
`mcp/claude.mcp.json` uses the same launcher for `kg_headers.py`.

PowerShell is a full path, not a fallback: it is on every Windows machine, and Git Bash is not.

- **Exit code.** PowerShell reports only success or failure for a command unless the command exits
  with the code itself, so the string ends in `; exit $LASTEXITCODE`. `sh` never reaches that
  part, because the sourced launcher ends in `exec` or `exit`. In the batch half each run ends in
  `call exit /b %%errorlevel%%`; a bare `exit /b` there returned 0 whatever the script returned.
- **Finding the script.** The batch half builds the script path from its own folder (`%~dp0`), so
  it needs no environment variable. The `sh` half uses `CLAUDE_PLUGIN_ROOT`, then the launcher's
  own path when the shell is bash, then the working folder, which is the plugin folder when Claude
  Code runs the headers helper.
- **No script policy.** The launcher is a `.cmd` file, so the PowerShell execution policy, which
  blocks `.ps1` files on a default Windows install, does not apply.
- **Tested** in Windows PowerShell 5.1 and PowerShell 7 (`test_hook_config.py`): a payload on
  stdin, the exit code, one run per call, a missing interpreter (exit 127), and plugin folders
  whose names hold a space, parentheses and `&`. A folder name with `%` is not supported by
  `cmd.exe`. Not tested: a Claude Code session on Windows without Git Bash. There is one launcher per script
because `sh` ignores arguments after a sourced file (`dash` does), and `test_hook_config.py` checks
that the seven are one template. The files keep LF endings (`.gitattributes`): `sh` cannot read a CR,
and the batch half has no labels or `goto`, which are what LF breaks in `cmd.exe`.

Claude Code also offers an exec form (`args`) that starts no shell. It is not used because `command`
must name one executable on every platform, and no interpreter name does: `py` exists only on Windows,
and `python3` is a Store stub there. `hooks/codex-hooks.json` keeps its `command` and `commandWindows`
pair, because Codex has that field.

## The search gate (PreToolUse)

The only part of the plugin that can **deny** a tool call, and the only reason the graph gets used at
all. Left alone, an agent greps and re-derives the map it already has.

A `Grep` / `Glob` — or a shell `grep`, `rg`, `fd`, `find -name`, `Get-ChildItem -Recurse` — aimed at
mapped code is denied, with instructions to run `kg_search` / `kg_node` / `kg_neighborhood` first.
It **keeps asking**: an earlier version stood down for the session after one nudge, which an agent
paid once before grepping freely for the rest of the turn.

A `PreToolUse` hook cannot add an argument to `Grep`, so there is no `force` flag — and that is the
better design, since a self-declared override is a rubber stamp an agent learns to always pass.
Three ways through, each inferred from what the agent actually did:

| Way through | What it means |
|---|---|
| **A query buys credit** | A codebase-kg MCP call clears the next `gate_credit` searches. The allowance is for what the answer did NOT name — a partial answer leaves a remainder only searching will find. The files it did name are located searches, which cost nothing. |
| **A located search** | A search scoped to a file the graph already anchors is never gated and spends no credit. The agent has evidently found it; gating that buys nothing. A *directory* is still gated — that is where you look when you do not yet know the file. |
| **Repeat to insist** | A search for the same thing as one already denied is allowed for the rest of the session: the same call, or the same pattern (a grep pattern, a `find -name` value, a Glob pattern) however the command around it is reworded. The last 16 denials are remembered, so searches denied in parallel do not evict each other. This is the escape hatch for code the graph has not mapped yet, and it is what makes the gate unable to strand anyone. |

- **No-ops** when the repo has no graph, when `SKIP_KG` is set, when `search_gate` is `off`, and when
  the search is scoped outside the graph's `root` or into an ignored dir (`node_modules`, `build`, …).
  An ignored dir stays in scope when a `covers` pattern of the graph can match a file beneath it, at
  any depth: `build`, `out`, `target`, `dist` and `vendor` do not hide a path that `covers` names. A
  pattern that opens with a wildcard, such as `**/*.py`, does not bring in `node_modules`, `.venv` or
  `vendor`.
- **Allows the search when the server is down.** A denial that nothing can clear strands the agent,
  because no query buys credit while no server answers. When the gate would deny and nothing answers as
  codebase-kg on the `server_port` port, it allows the search. It adds one notice per session that names
  `kg_cli.py server start`.
- **Leaves non-source searches alone.** A search whose file-name filters can only match files no graph
  anchors — `find -name Info.plist`, `--include='*.xcconfig'`, `Glob **/*.png`, anything scoped
  inside an `.xcodeproj` or `.xcassets` — is not a question for the graph. The list is
  `NON_SOURCE_EXT` in `kg_search_gate.py` plus the repo's `exclude_ext`.
- **Reads shell commands the way the shell does.** A flag's value (`-A 20`, `--include x`) is not a
  path, and an unexpanded `$VAR` or `$(…)` is no evidence either way, so neither can turn a grep of one
  known file into a "still hunting" denial. A heredoc body is data, not a command. The parser lives in
  `_shell_search.py`.
- **Fails open.** A malformed payload or an unreadable graph lets the search through. An unwritable
  state file degrades the deny to a **warn**, because the escape hatch lives in that file: a denial
  that cannot be recorded is one a repeat could not be recognised against.
- A query sets credit to `gate_credit` and never lowers credit already held, so a query that errors
  is still worth something, and a cheap follow-up cannot cost the allowance an earlier one earned.
- **Inside a subagent the gate informs instead of denying.** The hook cannot see a subagent's tool
  list, and a subagent without a codebase-kg tool could never earn credit, so a gated search from a
  subagent passes with the graph-first instruction added to its context.
- **`warn` mode** passes the search, adds the instruction to Claude's context, and shows it to you.

Shell detection is deliberately narrow — a false positive denies unrelated work. `find` and
`Get-ChildItem` only count when they carry a name/path filter, so an ordinary `find . -type d` is not
a search. Claude Code starts the gate for a shell call only when an `if` rule in `hooks.json` names
one of its subcommands, so `git status` costs no process. `test_hook_config.py` asserts that the
rules list every word the parser knows. A command that matches two rules starts two gate processes;
the first to claim the call's `tool_use_id` decides, and the other exits silently.

## The push gate (PreToolUse)

Denies a `git push` that an agent runs while any mapped file is stale against `HEAD`. The pre-push git
hook blocks the same push, but an agent that sees that block can retry with `--no-verify` or a
`SKIP_KG=1` prefix without anyone noticing. This hook denies earlier, in the agent's turn, and says
what to do: run `/codebase-kg:refresh`, commit the graph, push again. The reason lists up to 20 stale
files with the total count.

- **Detects the push by parsing the command.** It finds `git push` after `&&`, `||`, `;`, `|`, a
  newline, `cd <dir>`, `git -C <dir>`, `git -c k=v`, an env prefix (`SKIP_KG=1 git push`), `env`,
  `command`, `sudo`, `timeout`, `nice`, `stdbuf`, `xargs`, control keywords such as `if` and `then`,
  backticks, `eval`, `bash -c '…'` (also `-lc`), `cmd /c`, `pwsh -c` and `git subtree push`. A `#`
  starts a comment only at the start of a word, so `fix#12` hides nothing. A dry run (`--dry-run`,
  `-n`) and `--help` send nothing and pass.
  `git commit -m "push"`, `git stash push` and `echo git push` are not a push, and a heredoc body is
  not scanned. A Git Bash path such as `/c/Users/…` after `cd` or `git -C` resolves to its Windows
  drive path. Claude Code starts the hook only for Bash commands that match `Bash(git *)` and
  PowerShell commands that match `PowerShell(git *)`. For PowerShell the hook reads `$env:NAME = value`
  as an env prefix, `Set-Location` and its aliases as `cd`, and a trailing backtick as a line join.
- **Uses the pre-push hook's comparison.** The hook loads `git-hooks/kg_pre_push.py` by path and
  compares every anchored file against `HEAD`, so both agree on what is stale.
- **Ways through.** `SKIP_KG=1`, or `KG_STALE_ACK=<n>` naming the stale count, in the command's env
  prefix (or an earlier `export`) or in the environment of the Claude Code process. A push that only
  deletes refs (`git push --delete`, `git push origin :branch`) passes, because it publishes no
  commits. `--no-verify` does not pass: it skips git hooks, and the point is that an agent cannot skip
  the check silently.
- **Fails open.** A parse error, an unreadable graph or any other error allows the push. The git hook
  still blocks it.
- **The agent refreshes.** When this hook denies, the agent runs `/codebase-kg:refresh` in its own
  turn. The pre-push hook runs no refresh of its own. When the agent pushes again with a current graph, nothing is stale
  and neither hook acts.

## The post-edit nudge (PostToolUse)

**Advisory.** Never blocks, never edits, fail-safe (any error exits silently so an edit is never
broken). Two signals, strongest first:

1. **The edited file is not anchored by any node.** A fact, not a guess — the map has a hole exactly
   where you're working. Reported the first time you touch that file, once per file. The graph's
   `covers` and `exempt` patterns decide whether the file counts as source.
2. **`nudge_every` edits to already-mapped files** (default 5). A weaker heuristic for descriptions
   drifting.

Also:

- The counter **resets** when the graph file changes, whether you edit it or `kg_upsert_node` or the
  CLI writes it (you kept it in sync).
- **No-ops** when the repo has no graph, when `post_edit_nudge: false`, and when the edited file is a
  doc/config (`.md`, `.json`, …) or lives in an ignored dir (`.git`, `node_modules`, `build`, …). The
  extension and directory lists apply only when the graph declares no `covers`.
- Does **not** honor `SKIP_KG`. To silence it, set `post_edit_nudge: false`.

Both hooks keep state in the OS temp dir — the nudge keyed by project, the gate by project +
session. **Nothing is written into the repo.**

## The unwired-clone notice (SessionStart)

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`, which **git never
clones**. So a repo can commit the checkers, the wrappers, `install.sh` and the `.gitattributes`
line, and every fresh checkout still starts with the hooks inert and the graph diffing as "Binary
files differ" — with no error anywhere to say so. This hook is the one thing that notices.

It prints one line naming the command, and does nothing else:

```
codebase-kg: this clone is not wired for code_graph.db (git hooks and graph diffs).
git does not clone .git/config, so run:  sh .githooks/install.sh
```

**It never wires a clone, and it must not.** Git leaves `.git/config` out of a clone on purpose:
cloning a repo must not be able to make it execute code. A plugin that set `core.hooksPath` on the
user's behalf would route around that protection and make the repo's vendored `.githooks/*.py` live
in a fresh clone without anyone choosing to run them. A plugin the user installed may suggest; it may
not decide.

It makes two writes, both to wiring someone already installed, and prints one line for each:

- **The diff driver's release.** When `diff.codegraph.textconv` pins a codebase-kg tag older than
  the installed plugin, the hook asks the remote for the installed version's tag
  (`git ls-remote --tags`, no credential prompt) and, when the tag is published, sets the
  textconv to it. When the tag cannot be confirmed it changes nothing, and the next session tries
  again. It says nothing while the older pin still exports the graph. When the pin is too old to
  read the graph's schema (`SCHEMA_SINCE` in the hook), it prints one line saying that `git diff`
  on the graph fails until the pin moves. The committed `install.sh` keeps
  its own pin until someone re-runs `/codebase-kg:setup`.
- **Codex's config.** `codex-hooks.json` runs the hook with `--codex`. `_codex_config.py` then moves
  the launcher path in `[mcp_servers.codebase-kg]`, and a codebase-kg `CLAUDE_PLUGIN_ROOT` in
  `[shell_environment_policy.set]`, to this plugin's folder in `~/.codex/config.toml` (or
  `$CODEX_HOME/config.toml`). A path moves only when its folder is missing or holds an older
  release. Every other line is written back unchanged. Codex may connect to MCP servers before the
  hook runs, so the first session after an update can need a restart.

Silent unless **all** of these hold:

- inside a git work tree;
- the graph (`graph_path`) exists;
- a hooks dir with both vendored checkers exists (`core.hooksPath` if set, otherwise `.githooks/`);
- `core.hooksPath` is unset, **or** `diff.codegraph.textconv` is unset, **or** the textconv names a
  codebase-kg folder that no longer exists. The last names `/codebase-kg:setup`.

And never when `core.hooksPath` points at a folder that lacks the vendored checkers — that repo made
a deliberate choice, and nagging it toward clobbering its own config is worse than saying nothing.
`SKIP_KG` silences it like the rest, and that skips the Codex config refresh too. The hook's git
calls share one 8 s budget, inside the 10 s hook timeout, so an offline machine cannot overrun it.
Any error exits silently; a session never fails to start
because of this.

`compact` is left out on purpose: both `SessionStart` hooks run on `startup|resume|clear` only. A compact
does not stop the server or drop the session registration, so running the hooks again would only
repeat work and could repeat the notice.

## The server start (SessionStart)

Claude Code reaches the codebase-kg server over HTTP on the `server_port` setting (default 47821). It
tries to connect a few times in the first seconds of a session, once more as soon as the
`SessionStart` hooks finish, and then gives up for the session. This hook makes sure something is
listening by then:

- It asks `GET /health` on the port. When this build, or a newer codebase-kg build, answers, it does
  nothing.
- When nothing answers, or an older codebase-kg build does, it starts this plugin's server detached,
  under the same lock the shim uses, and waits up to 8 s for `/health` to answer. A newer server
  takes the port from an older one itself. On Windows the hook starts the server through WMI so the
  host's job object cannot end it with the session, and falls back to a plain detached start.
- When the port answers as something else, it prints one line naming the `server_port` setting.
- When the server does not answer in 8 s, it prints one line naming the server's log. The server
  imports only the standard library, so this means it failed to start or the machine is very slow.

The server it starts reads the `max_workers` setting from this hook's environment, once. To apply a
changed `max_workers`, run `kg_cli.py server stop` and restart Claude Code; the README's
"Changing `max_workers`" section has the steps. `kg_cli.py server start` starts the server by hand.
The server writes `server-<build>.log` in the cache folder. It records the pid that started it, the
signals it receives, a heartbeat every 10 minutes and a line at exit.

It also registers the session: it writes `sessions/<session_id>.json` in the cache folder with the
agent's pid, found by walking up from the hook past interpreters and shells. `kg_session_end.py`
deletes that file on `SessionEnd`. A server that has seen a registered session exits when no session
has been live, and nothing has used the server, for `CODEBASE_KG_SESSION_GRACE` seconds (default
300). A new session within that time keeps the server. The server also drops a session whose agent
pid has exited, so a `SessionEnd` hook that Claude Code cuts short, or a crashed agent, delays the
exit and does not prevent it. Codex has no `SessionEnd` hook, so `mcp/launch/kg_headers.py` registers
each Codex session as `codex-<pid>` when it runs with `--start`, and the pid check ends it.

It exits 0 every time and prints nothing when all is well. Stdlib only; it imports
`mcp/src/codebase_kg/shim.py` by path (`_shim_loader.py`) for the port, the build and the start routine.

## Config — optional per-dev override only (gitignored `.claude/codebase-kg.local.md`)

The shared config (`root`) is the committed graph's `meta` table. This file is **only** for a
per-developer override of hook behavior, `root`, or `graph_path`; it is gitignored, not committed.
See `../templates/codebase-kg.local.md.example`.

| key | default | meaning |
|---|---|---|
| `search_gate` | `block` | `block` \| `warn` (message, no deny) \| `off` |
| `gate_shell_search` | `true` | also gate `grep`/`rg`/`find -name` run through a shell |
| `gate_credit` | `3` | searches one graph query clears |
| `post_edit_nudge` | `true` | master off-switch for the nudge |
| `nudge_every` | `5` | edits to mapped files between periodic nudges |
| `root` | from the graph | only paths under here count |
| `graph_path` | `knowledge/code_graph.db` | where the graph lives (`kg_path` is an alias) |

`SKIP_KG=1` in the environment silences the search gate, the push gate, the unwired-clone notice (and the
Codex config refresh it runs) and the `pre-commit` and `pre-push` checks for that shell, for a change that deliberately outruns the
graph. It does not silence the post-edit nudge, the server start or the session end.

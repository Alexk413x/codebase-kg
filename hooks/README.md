# hooks/

Four hook scripts. All ship with the plugin — there is **nothing to install per repo**.
The search gate, the push gate and the unwired-clone notice no-op in a repo with
no `knowledge/code_graph.db`. The server start runs in every session.

| File | Role |
|---|---|
| `hooks.json` | Plugin hook config for Claude Code. Every gate is a hook of type `mcp_tool` on the plugin's second MCP server, `codebase-kg-hooks` (see "The hook relay"), so no process starts. PreToolUse on `Grep\|Glob\|Bash\|PowerShell` calls `kg_hook_pre`, which holds the search gate and the push gate. PostToolUse on `Bash(git *)` and `PowerShell(git *)` calls `kg_hook_commit_check`. PreToolUse on the six graph write tools calls `kg_hook_allow_write`. SessionStart on `startup\|resume\|clear` → `kg_server_start.py`, then `kg_session_start.py`. Every command hook is `. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/<script>.cmd"`; see "How a hook starts" below. |
| `codex-hooks.json` | The same hooks for Codex, named by `hooks` in `.codex-plugin/plugin.json`: the search gate and push gate on `Bash`, the credit hook on codebase-kg MCP calls, and the session notice. It has no `if`, `Grep`, `Glob` or `PowerShell` entry, because Codex ignores `if` and reports shell commands as `Bash`, so each script decides for itself whether a command is a search or a push. It has no server start hook: the `--start` option of `mcp/launch/kg_headers.py`, which the Codex config table runs as its headers helper, or the stdio shim starts the server. `commandWindows` runs `py -3` and names the script through `$env:PLUGIN_ROOT`, because Codex runs it in PowerShell, where `${CLAUDE_PLUGIN_ROOT}` is empty; `command` runs `python3`. |
| `copilot-hooks.json` | The hooks for GitHub Copilot CLI, named by `hooks` in `.github/plugin/plugin.json`: `preToolUse` runs `kg_copilot.py` with `python`. |
| `kg_copilot.py` | The Copilot CLI adapter. Turns Copilot's hook payloads into the shape the shared search gate and push gate read, so those scripts stay unchanged. |
| `../hooks.json` | The hooks for Antigravity CLI, which reads only a `hooks.json` at the plugin root: `PreToolUse` on `run_command` and `call_mcp_tool`, and `SessionStart`. Every handler is a `command` that tries `py -3`, `python3` and `python` and ends in `|| exit 0`, because `agy` skips the whole file for one handler that is not a `command`, and blocks the tool call when a hook exits non-zero. |
| `kg_antigravity.py` | The Antigravity CLI adapter. Turns `agy` payloads into the shape the shared gates read and answers `{"decision": "deny", "reason": ...}`. Sends the session notice as an injected system message. |
| `kg_search_gate.py` | The gate. Denies a search aimed at mapped code with the instruction to query the graph, and keeps doing it — a query buys credit, a located search is free, a repeat always passes. |
| `kg_push_gate.py` | The push gate. Denies an agent's `git push` while any mapped file is stale against `HEAD`. |
| `kg_server_start.py` | The server start, and the session register. Starts the shared HTTP server when nothing answers on the `server_port` setting, or a server this copy may replace does, and registers the session. One line only when the port belongs to another program, the server does not come up, or this is a development copy and another build holds the port. |
| `kg_session_start.py` | The unwired-clone notice. One line when this clone has the committed checkers but no `core.hooksPath` or no `diff.codegraph.textconv`. It never wires a clone. In a wired clone it moves the release the diff driver pins to the installed plugin's, and with `--codex` it runs `_codex_config.py`. |
| `_codex_config.py` | Moves the codebase-kg paths in Codex's `config.toml` to the installed plugin's folder after an update. |
| `_config.py` | Reads `root`, `covers` and `exempt` from the committed graph's `meta` table (auto-discovers `knowledge/code_graph.db`); a gitignored `.claude/codebase-kg.local.md` may override. Decides whether a path reaches mapped code. Holds the helpers the hooks share: state files, the deny output, the `SKIP_KG` test. |
| `_shell_search.py` | The search gate's shell-command parsing: which clauses of a Bash or PowerShell command are searches, where each looks and for what. The gate decision stays in `kg_search_gate.py`. |
| `_shim_loader.py` | Loads `mcp/src/codebase_kg/shim.py` by path, for the hooks that start or register with the server. |

## How a hook starts

Claude Code runs a shell-form hook command with `sh -c` on macOS and Linux, in Git Bash on Windows,
and in PowerShell on Windows when Git Bash is not installed (it falls back to `powershell.exe`, 5.1,
when there is no `pwsh`). One command string has to work in all of them, so each hook is

```
. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/kg_push_gate.cmd"; exit $LASTEXITCODE
```

`.` sources the file in `sh` and runs it in PowerShell (PowerShell resolves the placeholder from
`${env:CLAUDE_PLUGIN_ROOT}`). The file is one text that both read. The first line, `:<<"::KG_CMD"`,
opens a here-document in `sh` that hides the batch lines, and is a label to `cmd.exe`, which runs the
batch lines when PowerShell starts the `.cmd`. Each half probes `py -3`, then `python3`, then `python`
by running it with `-c ""`, so the Microsoft Store `python3` stub is skipped, and then runs the script
once: a script that exits non-zero is not retried under another interpreter.

The headers helper in `mcp/claude.mcp.json` uses the same launcher for `kg_headers.py`, through a
different command. Claude Code starts the helper with Node's `shell: true`, which is `cmd.exe` on
Windows and `/bin/sh` elsewhere, never Git Bash. `cmd.exe` cannot run `. "<file>"`, so the helper is

```
call="${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/kg_headers.cmd"; . "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/kg_headers.cmd"; exit $LASTEXITCODE
```

Claude Code replaces the placeholder with the plugin folder before it starts the shell. `cmd.exe`
treats `=` and `;` as word separators, so it reads the first statement as `call "<file>"` with the
rest of the line as arguments, which the launcher ignores. `sh` reads `call="<file>"` as a variable
assignment, which prints nothing, and then sources the launcher, which finds the plugin folder from
`$call` when `CLAUDE_PLUGIN_ROOT` is not set. PowerShell, if a user makes it the
shell, reports `call=...` as an unknown command on stderr and runs the second statement. In every
shell the launcher runs once, prints one JSON object, and exits 0. `cmd.exe` `call` doubles a `^`
in the path, so a plugin folder whose path holds `^` or `%` does not work there.
`test_hook_config.py` runs the configured string through `cmd.exe` as Node starts it
(`cmd.exe /d /s /c "<command>"`), and through `sh`, `bash`, `dash`, Windows PowerShell 5.1 and
PowerShell 7.

PowerShell is a full path, not a fallback: it is on every Windows machine, and Git Bash is not.

- **Exit code.** PowerShell reports only success or failure for a command unless the command exits
  with the code itself, so the string ends in `; exit $LASTEXITCODE`. `sh` never reaches that
  part, because the sourced launcher ends in `exec` or `exit`. In the batch half each run ends in
  `call exit /b %%errorlevel%%`; a bare `exit /b` there returned 0 whatever the script returned.
- **Finding the script.** The batch half builds the script path from its own folder (`%~dp0`), so
  it needs no environment variable. The `sh` half uses `CLAUDE_PLUGIN_ROOT`, then the launcher's
  own path when the shell is bash, then the folder above `$call`, which the headers helper command
  sets. When none of them is set it prints `cannot find the plugin folder` and exits 127. It never
  uses the working folder, because a hook runs in the project.
- **No interpreter from the working folder.** `cmd.exe` looks for a command in the working folder
  before `PATH`, so a `py.bat` in a project would run in place of the real `py`. The batch half sets
  `NoDefaultCurrentDirectoryInExePath=1` before it starts any interpreter, which turns that lookup
  off. `mcp/launch/kg-shim.cmd` does the same.
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

## The hook relay (Claude Code)

A hook of type `mcp_tool` that names a server which is not connected, does not know the tool, or
answers with an error fails, and Claude Code prints a two-line notice for each failed hook on every
tool call. A hook that gets no answer waits for its whole timeout. Nothing in a hook's config makes
either quiet. So the hooks do not name the shared HTTP server. They name `codebase-kg-hooks`, a
second MCP server in `mcp/claude.mcp.json` that Claude Code starts on stdio with each session
(`mcp/launch/kg-hooks`, which runs `shim.py --hooks`; the class is `HookRelay` in
`mcp/src/codebase_kg/slipspace/bridge.py`).

- **What it does.** It passes each hook call to the shared server, with the token and the agent's
  process id, and returns the answer unchanged. It lists no tools and sends no instructions, so it
  adds nothing to the model's context.
- **When the server gives no hook result.** No listener, a refused token, an older build that does
  not know the tool, an error, or no answer in time: the relay answers `{}`, so the tool call goes
  on. Every gate fails open; the git pre-push hook still blocks a stale push.
- **What the user sees.** One line when the gates go off and one when they return, and nothing in
  between. A healthy session shows nothing.
- **Allowances.** 1.5 s for a search decision, 20 s for a push or a commit check, which run git.
  After a call that got no answer in time, the relay sends nothing for 30 s, so a server that hangs
  costs one wait. A command that may hold a `git push` is the exception: it is always sent, so a
  search that timed out does not let the next push through unchecked. After any other failure it starts the server on another thread, the way the
  SessionStart hook does, and asks again as soon as one runs. That is also what brings the gates
  back after a plugin update: `/reload-plugins` runs no SessionStart hook, and the relay of the new
  version replaces the old server.
- **Launch.** `claude.mcp.json` names `mcp/launch/kg-hooks` with no extension: Claude Code runs the
  `sh` script on macOS and Linux and `kg-hooks.cmd` on Windows. Both run the base interpreter with
  `-I -S`.
- **A hook the server does not know.** A server from this version on answers an unknown
  `kg_hook_*` tool with an empty result and a mark in `_meta`. The relay counts that as a failure,
  so it never reports the gates as back on while the server runs none of them.
- **Sessions that share a credit count.** A client that cannot find its agent process sends
  `unknown`, and all such sessions of one user share one count: a graph query in one of them can
  clear the next gated search in another. It happens where the process table cannot be read or
  the agent sits more than 12 processes above the helper; no client is known to hit it on Windows,
  macOS or Linux as tested. The count is lenient by design, so a query always earns credit.
- **No Python.** The relay cannot start, and Claude Code prints its notice for every hook call, as
  it does for every hook of every plugin that needs an interpreter.

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
| **A located search** | A search that names its files is never gated and spends no credit, whether or not the graph anchors them and whether or not they exist yet (`cp a b && grep x b`). The agent has evidently found the file; gating that buys nothing. A *directory* is still gated when the graph anchors code beneath it — that is where you look when you do not yet know the file. |
| **Repeat to insist** | A search for the same thing as one already denied is allowed for the rest of the session: the same call, or the same pattern (a grep pattern, a `find -name` value, a Glob pattern) however the command around it is reworded. The last 16 denials are remembered, so searches denied in parallel do not evict each other. This is the escape hatch for code the graph has not mapped yet, and it is what makes the gate unable to strand anyone. |

- **No-ops** when the repo has no graph, when `search_gate` is `off`, when `SKIP_KG` is set (script
  only, see "Where the gate runs"), and when
  the search is scoped outside the graph's `root`, outside a declared `covers` or inside `exempt`, into
  a directory the graph anchors nothing beneath (docs, scratch folders), or into an ignored dir
  (`node_modules`, `build`, …). Relative paths in a shell command resolve from the shell's own
  directory, then follow `cd`, `pushd` and `Set-Location`. A denial names the folder and the rule.
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
  known file into a directory search. A heredoc body is data, not a command. A `cd` to a place only
  the shell can resolve (`cd "$DIR"`, a bare `cd`) takes the relative paths after it out of scope.
  `Select-String` reads `-Path` and `-Pattern` by name. A `Glob` with no `path` and an absolute or `~`
  pattern is scoped to the pattern's fixed prefix. The parser lives in `_shell_search.py`.
- **Says when nothing ran.** A hook can only refuse a whole shell call. When a denied command holds
  other parts, such as `cat >> notes.md <<EOF … EOF && grep -rn x .`, the denial opens with
  `nothing in this command ran` and names those parts, so the agent reruns them on their own.
- **Fails open.** A malformed payload or a parser error lets the search through. A graph file it cannot
  read keeps the gate on: it reads as the whole repo, not as a folder with no code. An unwritable
  state file degrades the deny to a **warn**, because the escape hatch lives in that file: a denial
  that cannot be recorded is one a repeat could not be recognised against.
- A query sets credit to `gate_credit` and never lowers credit already held, so a query that errors
  is still worth something, and a cheap follow-up cannot cost the allowance an earlier one earned.
- **Inside a subagent the gate informs instead of denying.** The hook cannot see a subagent's tool
  list, and a subagent without a codebase-kg tool could never earn credit, so a gated search from a
  subagent passes with the graph-first instruction added to its context.
- **`warn` mode** passes the search, adds the instruction to Claude's context, and shows it to you.
- **Keeps state outside the repo**, keyed by project and session: in the server's memory for Claude
  Code, in the OS temp dir for the script. Nothing is written into the repo.

Shell detection is deliberately narrow — a false positive denies unrelated work. `find` and
`Get-ChildItem` only count when they carry a name/path filter, so an ordinary `find . -type d` is not
a search.

### Where the gate runs

One rule, two callers. The decision is `kg_search_gate.py` for every client.

- **Claude Code** asks the server. `hooks.json` has one `mcp_tool` entry for `Grep|Glob|Bash|PowerShell`,
  which calls the unlisted tool `kg_hook_pre` through the hook relay. The server hands a `git push` to
  the push gate first and everything else to the search gate (`core.pre_tool`,
  `mcp/src/codebase_kg/search_gate.py`). The server loads `kg_search_gate.py` and replaces the parts
  that assumed a process of their own: state lives in memory, and the server counts graph queries
  itself, because it receives them. No hook reports a query. A model's query arrives on the
  HTTP connection and the hook call that follows arrives through the relay, two clients. Both name the
  agent process they belong to (`X-Codebase-KG-Agent`), which is how the credit reaches the right
  session, for the main agent and for a subagent. Clients that cannot find their agent process say
  `unknown` and share one count, so a query always earns credit. A server
  restart clears the state: denied searches are asked once more and the credit starts from zero. A
  session that is older than the server is told so once.
- **Codex, Copilot CLI and Antigravity CLI** run the script as a process (`codex-hooks.json`, `kg_copilot.py`, `kg_antigravity.py`). There a
  second hook on the codebase-kg tools credits a query, state lives in the OS temp dir, and several
  matching handlers settle who decides through a claim file on the call's `tool_use_id`.

With the server unreachable, the hook relay answers for it and the search runs. The script allows the
search and says so once.

`SKIP_KG` differs by caller. For Claude Code it does not apply: a direct hook passes no environment,
and the server ignores `SKIP_KG` written in front of a search. `search_gate: off` in the per-clone file
is the off switch, and repeating a denied search is the agent's own way through. The script reads
`SKIP_KG` from its environment.

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
  drive path. Claude Code calls the hook only for Bash commands that match `Bash(git *)` and
  PowerShell commands that match `PowerShell(git *)`. For PowerShell the hook reads `$env:NAME = value`
  as an env prefix, `Set-Location` and its aliases as `cd`, and a trailing backtick as a line join.
- **Uses the pre-push hook's comparison.** The hook loads `git-hooks/kg_pre_push.py` by path and
  compares every anchored file against `HEAD`, so both agree on what is stale.
- **A skip is the user's decision (Claude Code).** `hooks.json` asks the server
  (`kg_hook_push_gate`, `mcp/src/codebase_kg/push_gate.py`), which reads the command with this script's
  parser. A push whose own prefix carries `SKIP_KG=1`, or `KG_STALE_ACK=<n>` naming the stale count
  (also after an earlier `export`), does not pass by itself. The server answers
  `permissionDecision: "ask"`, so Claude Code shows its permission prompt with the stale files listed,
  and the push runs only when the user says yes. The prompt appears in a session that runs with
  permissions bypassed too. A headless or background run has no prompt, so the push is denied. A command
  that pushes several repositories lists every one. The server ignores `SKIP_KG` and `KG_STALE_ACK` in
  its own environment.
  The question is not an MCP elicitation, although a hook can raise one: Claude Code keeps the hook
  waiting while that dialog is open, and when the hook's timeout passes it cancels the hook and runs the
  command. The permission prompt has no timeout.
- **Ways through (Codex, Copilot CLI).** The script lets the same prefixes through without asking, and
  reads them from the environment of its own process too.
- **A client with no permission prompt.** The gate takes `prompts: false` in its arguments
  (`kg_hook_push_gate`, and `kg_hook_pre`, which hands it on). It then answers `deny` for a skip
  prefix too and the reason offers no prefix, because a client that does not honor `ask` runs the
  command. Codex is such a client; see "Codex: gates answered by the server".
- **Never a way through.** `--no-verify`: it skips git hooks, and the point is that an agent cannot skip
  the check silently. A push that only deletes refs (`git push --delete`, `git push origin :branch`)
  passes, because it publishes no commits.
- **Fails open.** A parse error, an unreadable graph, any other error, or an unreachable server allows
  the push. The git hook still blocks it. The hook sets no `onFailure: "block"`: it runs for every git
  command, so blocking on failure would stop `git status` when the server is down.
- **The agent refreshes, without asking.** When this hook denies, the reason tells the agent to run
  `/codebase-kg:refresh`, commit the graph and push again without asking the user. A second hook answers
  `allow` for the six graph write tools (`kg_hook_allow_write`), so the refresh does not stop at a
  permission prompt: a graph update changes no code, and the graph file is in git. Deny and ask rules in
  your own settings still apply. The pre-push hook runs no refresh of its own. When the agent pushes
  again with a current graph, nothing is stale and neither hook acts.

## The commit check (PostToolUse)

Claude Code only. It has no script: `hooks.json` calls the codebase-kg server, through the hook relay, with a hook of
type `mcp_tool`, so no process starts. The server answers the tool `kg_hook_commit_check`, which
`tools/list` does not show (`mcp/src/codebase_kg/commit_check.py`).

- **When.** After a `Bash` or `PowerShell` command that runs git. The hook runs after the command
  because `git add -A && git commit` and `git commit -am` stage and commit in one command, and a hook
  that runs first reads an empty index.
- **What it checks.** The commits the command made, read from the `HEAD` reflog: the newest entries
  `git commit` wrote (plain, initial, amend and merge), at most 120 seconds old, that the server has
  not reported, as one range. A failed `git commit` writes no entry, so the check says nothing.
  Commits a rebase or a pull brought in are not counted. A repository with no reflog falls back to
  the commit at `HEAD`.
- **Which repository.** The session folder, or the folder the command names with `git -C <dir>` or a
  `cd` before the commit. `--git-dir`, `--work-tree` and a commit made inside a script are not followed.
- **What the agent gets.** `additionalContext` that lists the committed source no node covers, the
  deleted source the graph still anchors on and the mapped source that changed since the graph was
  updated, with the instruction to run `/codebase-kg:refresh` and commit the graph in a follow-up
  commit before pushing, without asking the user. The comparison is the one in
  `git-hooks/kg_pre_push.py`.
- **Advisory, fails open.** It never blocks. An error, a repo with no graph or an unreachable server
  says nothing. The push gate still blocks a push while a mapped file is stale.
- **Off switch.** `commit_check: false` in `.claude/codebase-kg.local.md`. `SKIP_KG` does not reach it:
  a direct hook passes no environment.

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
- **Codex hook trust.** Codex runs none of the hooks in `codex-hooks.json` until the user trusts them
  with `/hooks`, and its trust covers a hash of every file in the plugin, so each plugin update asks
  again. An untrusted hook is skipped without a message, this SessionStart hook included, so no hook
  can report it. `kg_cli.py codex-config` asks Codex (`codex app-server`, `hooks/list`) and prints
  the status (`mcp/src/codebase_kg/codex_trust.py`). Nothing in the plugin writes Codex's trust state.
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

- It asks `GET /health` on the port. When this build answers, or a build it may not replace, it
  starts nothing and the session uses that server.
- Which server it may replace is in `mcp/README.md`, "Which build holds the HTTP port": a release or
  an installed copy replaces a lower version, and a development copy replaces nothing. A development
  copy that finds another build on the port prints one line with that build's version and the
  `server_port` setting.
- When nothing answers, or a server it may replace does, it starts this plugin's server detached,
  under the same lock the shim uses, and waits up to 8 s for `/health` to answer. The new server
  takes the port from the old one itself. On Windows the hook starts the server through WMI so the
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
agent's pid, found by walking up from the hook past interpreters and shells. A server that has seen
a registered session exits when no session has been live, and nothing has used the server, for
`CODEBASE_KG_SESSION_GRACE` seconds (default 300). A new session within that time keeps the server.
The server drops a session whose agent pid has exited, which is how a session ends: no hook
unregisters it, so a closed, killed or crashed agent is handled the same way. A session registered
with no pid stays for a fixed time. `mcp/launch/kg_headers.py` registers each Codex session as
`codex-<pid>` when it runs with `--start`, and the stdio bridge registers `bridge-<pid>`.

It exits 0 every time and prints nothing when all is well. Stdlib only; it imports
`mcp/src/codebase_kg/shim.py` by path (`_shim_loader.py`) for the port, the build and the start routine.

## Codex: gates answered by the server (not the default)

`codex-hooks.json` runs the gate scripts, as in 0.17. The server can answer Codex's gates too,
through `mcp_tool` hooks on the server named `codebase-kg` in `config.toml`. That path is tested
with hooks passed as session flags (Codex 0.167.2, Windows): a gated search is blocked, a graph
query clears the next one, a stale push is blocked with or without a skip prefix, and every
failure of the hook lets the command run. It is not the plugin's default, because a plugin's
`mcp_tool` hooks need the same trust as its script hooks and Codex has no relay: a server that is
away shows as a failed hook on every command.

To use it on a machine you manage, declare the hooks as managed hooks, which Codex trusts by
policy and keeps across plugin updates. On Windows the file is
`%ProgramData%\OpenAI\Codex\requirements.toml` (administrator rights); on macOS and Linux
`/etc/codex/requirements.toml`. This text is not run as a managed file here:

```toml
[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "mcp_tool"
server = "codebase-kg"
tool = "kg_hook_pre"
timeout = 25
input = { tool_name = "Bash", tool_input = "${tool_input}", cwd = "${cwd}", session_id = "${session_id}", prompts = false }

[[hooks.PostToolUse]]
matcher = "^Bash$"

[[hooks.PostToolUse.hooks]]
type = "mcp_tool"
server = "codebase-kg"
tool = "kg_hook_commit_check"
timeout = 25
input = { command = "${tool_input.command}", cwd = "${cwd}" }
```

- **A file Codex cannot read stops Codex.** Keep a copy of the previous file, and start Codex
  right after you save.
- **Do not also trust the plugin's own hooks.** Managed hooks add to plugin hooks, so with both
  trusted each gate runs twice, once as a script and once on the server.
- **Check.** In a repository with a graph, `codex exec "run the shell command: grep -rn x src"`
  prints `Command blocked by PreToolUse hook: codebase-kg: denied a search`.
- Each `input` holds only fields every event has. A placeholder for a missing field fails the hook,
  and a failed hook lets the command run. `prompts = false` must stay: without it a skip prefix
  gets `ask`, which Codex does not honor.

## Config — optional per-dev override only (gitignored `.claude/codebase-kg.local.md`)

The shared config (`root`) is the committed graph's `meta` table. This file is **only** for a
per-developer override of hook behavior, `root`, or `graph_path`; it is gitignored, not committed.
See `../templates/codebase-kg.local.md.example`.

| key | default | meaning |
|---|---|---|
| `search_gate` | `block` | `block` \| `warn` (message, no deny) \| `off` |
| `gate_shell_search` | `true` | also gate `grep`/`rg`/`find -name` run through a shell |
| `gate_credit` | `3` | searches one graph query clears |
| `commit_check` | `true` | `false` turns off the commit check |
| `root` | from the graph | only paths under here count |
| `graph_path` | `knowledge/code_graph.db` | where the graph lives (`kg_path` is an alias) |

`SKIP_KG=1` in the environment silences the push gate, the unwired-clone notice (and the
Codex config refresh it runs), the `pre-commit` and `pre-push` checks, and the search gate where it runs
as a script (Codex, Copilot CLI), for a change that deliberately outruns the graph. In Claude Code the
search gate runs in the server and `SKIP_KG` does not apply to it: set `search_gate: off` in the
per-clone file. It does not silence the server start or the commit check.

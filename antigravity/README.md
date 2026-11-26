# Antigravity CLI support

Antigravity CLI (`agy`) reads its own manifest and a hooks file at the plugin root.
A hook adapter translates `agy` payloads for the shared search gate and push gate.
The Claude Code, Codex and Copilot CLI manifests and hooks stay unchanged.

Tested on `agy` 1.5.18 on Windows. Not tested on macOS or Linux.

## Load the plugin

Install Python 3.9 or later. `agy` runs each hook command through `cmd /c` on
Windows and `sh -c` elsewhere, in the plugin folder. The command tries `py -3`,
then `python3`, then `python`, so the Microsoft Store `python3` stub does not stop it.

Put the plugin folder where `agy` discovers plugins:

- `<repo>/.agents/plugins/codebase-kg` for one repository.
- `~/.gemini/config/plugins/codebase-kg` for every repository.

`agy` finds `.antigravity-plugin/plugin.json` and loads the seven shared skills and
`hooks.json` from the plugin root. It reads no manifest field for hooks, and it never
reads `hooks/hooks.json`, which is the Claude Code file. Do not load a second copy of
the plugin in the same session.

The shared skills describe Claude-style tool names and shell commands. A skill's
`allowed-tools` does not replace `agy` permissions.

## Add the MCP server

The plugin declares no MCP server. `agy` starts a plugin-owned server in the plugin
folder, and the stdio shim routes by its working folder. A server from the global
configuration starts in the session folder, so each repository resolves its own graph.

```sh
agy mcp add codebase-kg python -- -I -S "<plugin folder>/mcp/src/codebase_kg/shim.py"
```

Use the shim inside the plugin folder that `agy` loads. The gate finds the server by
its build, and the build includes the shim's path. A shim from another copy starts a
different server, and the gate then allows every search.

The shim starts the shared server when none of its build runs. Which copy may hold the
shared HTTP port (47821) follows the rule in `mcp/README.md`, "Which build holds the
HTTP port":

- A copy under `~/.gemini/config/plugins` is an installed copy. Its server replaces a
  lower version on the port and leaves the port to the same or a higher version.
- A copy under `<repo>/.agents/plugins` is a development copy unless it is a release
  build. Its server never takes the port from another build.

In both cases `agy` works without the HTTP port: the shim and the hook adapter reach
their server through its stdio listener.

Name the server `codebase-kg`. A call to a tool of that server earns search credit.
A server named `<plugin>_codebase-kg` works too. Another server name earns none.
`python` must resolve to Python 3.9 or later.

## Hooks

| Event | Matcher | Effect |
|---|---|---|
| `PreToolUse` | `run_command\|call_mcp_tool` | The search gate and push gate decide on a shell command. A `kg_search`, `kg_node`, `kg_neighborhood` or `kg_find_by_*` call earns credit. |
| `SessionStart` | none | The unwired-clone notice, injected as a system message. |

The adapter is `hooks/kg_antigravity.py`. It reads the JSON that `agy` writes to stdin
and answers `{"decision": "deny", "reason": "..."}` to block a call. It prints nothing
to allow one.

- **Tool names.** `agy` has no grep or glob tool. It searches through `run_command`,
  which runs Windows PowerShell on Windows and `sh` elsewhere. The adapter hands the
  command to the gates as `PowerShell` or `Bash` to match.
- **Fails open.** A hook that exits non-zero or prints text that is not JSON blocks
  the tool call in `agy`. The adapter exits 0 on any error, and each command ends in
  `|| exit 0`, so a machine without a working Python allows the call.
- **Handler types.** The hooks file holds only `command` handlers. One handler type
  that `agy` refuses (`mcp_tool`, `http`, `agent`) makes it skip the whole file.
- **No notices on a tool call.** A `PreToolUse` answer has no field for context.
  The server-unavailable notice, `warn` mode and the subagent note pass the call
  without a message. A denial reaches the agent as `tool call denied by pre-tool
  hook: <reason>`.
- **Credit and push.** A query earns three searches per conversation by default.
  A repeated denied search passes. A push prefixed with `SKIP_KG=1` passes, as in
  Codex and Copilot CLI.
- **Timeouts** in `hooks.json` are seconds.
- **Relative script path.** Each command names `./hooks/kg_antigravity.py`. `agy` runs a plugin's
  hook in the folder that holds `hooks.json`, which is the plugin folder, and substitutes no variable
  into the command, so a relative path is the form its documentation gives. The path never resolves
  in the project, so a repository cannot plant the script.

`agy plugin validate --strict` reports one warning for this plugin: `hooks/hooks.json`
is never loaded. That file is the Claude Code hook list, so the warning is expected.

## Support boundaries

This setup targets the Antigravity CLI. It does not configure Antigravity IDE or
Antigravity 2.0, which have their own hook locations. It does not widen sandbox
permissions.

The release workflow keeps `.antigravity-plugin`, `hooks.json` and the adapter.
Marketplace entries that pin the `release` branch receive them after a release
publishes the branch.

The hook format is in the `agy-customizations` skill that ships with `agy`
(`docs/hooks.md`).

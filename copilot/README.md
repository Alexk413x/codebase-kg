# GitHub Copilot CLI support

Copilot CLI uses a separate manifest, stdio configuration printer, and hook adapter.
The Claude Code and Codex manifests, hooks, and HTTP setup remain unchanged.

## Load the plugin

Install Python 3.9 or later. For plugin loading, `python` must resolve to that
interpreter on `PATH`, including on Windows. The MCP and hook commands execute
Python directly. They do not execute a `.cmd` file or depend on a POSIX shell.

First generate the repository's MCP configuration as described below. Then,
from that repository, load a local checkout or unpacked release:

```powershell
copilot --plugin-dir "<codebase-kg folder>"
```

Trust the working folder when Copilot asks. Copilot discovers
`.github/plugin/plugin.json` before the Claude manifest. It loads the seven
shared skills and `hooks/copilot-hooks.json`. The separate project MCP
configuration supplies the graph tools.
Do not install a second copy of this plugin in the same session.

The shared skills describe Claude-style tool names and shell commands.
Copilot supports those tool-name aliases, but a skill's `allowed-tools` does
not replace Copilot permissions. If a command references `CLAUDE_PLUGIN_ROOT`
and your shell does not define it, use the absolute plugin folder instead.
In PowerShell, environment variables use `$env:CLAUDE_PLUGIN_ROOT`, not
`${CLAUDE_PLUGIN_ROOT}`. You can also derive the plugin folder from the loaded
skill's path, two levels above `SKILL.md`.

## Configure only MCP, or choose an exact Python executable

The setup printer changes no files, starts no server, and modifies no global
settings. Run it with the Python interpreter you want Copilot to use:

```powershell
python "<codebase-kg folder>\mcp\launch\kg_copilot.py" mcp
python "<codebase-kg folder>\mcp\launch\kg_copilot.py" hooks
```

The `mcp` output is a complete JSON document. Merge its `mcpServers.codebase-kg`
entry into the target repository's `.mcp.json` or `.github/mcp.json`. Do not
overwrite other servers. The `hooks` output is a separate configuration for
`.github/hooks/codebase-kg.json`. Use it only when you are **not** loading the
plugin's hooks, or hooks run twice. Both outputs use the running interpreter's
absolute path. `--python <executable>` overrides it.

These machine-specific paths are suitable for local checkout configuration.
For committed configuration, each collaborator needs an accessible package
folder and Python executable. Regenerate configuration after moving Python or
the package. MCP-only configuration exposes the tools, not the skills.

Do not point Copilot at `mcp/claude.mcp.json`. That file uses Claude's
`user_config.server_port` and `headersHelper` extensions. A raw HTTP URL alone
does not supply authentication or session repository routing.

## Routing and lifecycle

The project MCP configuration starts the stdio shim in the session's working directory. The shim
uses the authenticated shared server and sends that directory in its handshake.
It does not pin the server to the plugin's repository. Two sessions in different
repositories resolve separate graphs. `CODEBASE_KG_PATH` remains an explicit
override when supplied to the server environment.

The Copilot manifest deliberately declares no MCP server. Copilot CLI starts
plugin-owned MCP commands in the plugin folder, which would route a cwd-based
shim to the wrong graph. Project-level configuration avoids that behavior.

The shim starts the server on demand, reuses the shared implementation's Windows
launch handling, and reconnects after a server crash. An in-flight request can
return a restart error and needs a retry. If the shared server cannot start,
the shim falls back to a private server. No Copilot SessionEnd hook stops a
server that other clients use. The shared server's idle timeout handles cleanup.

The separate hook adapter translates Copilot arguments and output. It reuses the
search and push gates and the edit nudge. Graph queries earn three searches by
default; other graphs and write calls do not earn credit. A repeated denied
search still passes. The adapter checks the authenticated stdio listener rather
than assuming the optional HTTP port represents tool availability. If that
listener is unavailable, the gate allows searches with a reconnect notice.
This also permits searches when the shim uses its private fallback.

## Support boundaries and distribution

This setup targets **Copilot CLI**. It does not configure VS Code, JetBrains,
Copilot cloud agent, or desktop-app-managed MCP servers. Those hosts have
different configuration and lifecycle contracts. Sandboxed CLI sessions also
need access to the package folder, Python, the graph, and the per-user server
cache. This setup does not widen sandbox permissions.

The release workflow preserves `.github/plugin`, the adapters, and the shared
skills. Marketplace entries that pin the `release` branch receive these files
only after this change merges and that workflow publishes the branch. A local
checkout check does not prove a marketplace install contains the change.

If Sentinel Swarm cannot find a Copilot-only package through its Claude plugin
registry lookup, set `SENTINEL_SWARM_KG_ROOT` to the **package root** containing
`mcp`, `hooks`, and `skills`. Do not point it at `.github/plugin`, an individual
skill, or a graph database. This setup does not change Sentinel's registry or
its graph-write locking and rebaseline contract.

References:
[MCP setup](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers),
[hooks](https://docs.github.com/en/copilot/reference/hooks-reference), and
[plugin discovery](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference).

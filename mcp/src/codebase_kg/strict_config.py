"""Print the MCP config a Claude Code session started with --strict-mcp-config needs:
kg_cli.py strict-mcp-config [--port N]

Such a session loads the plugin's hooks but none of its MCP servers. The hooks address the
hook relay by the name Claude Code gives a plugin's server, so the session must pass a server
of exactly that name, or every hook call fails and no gate runs:

    claude --strict-mcp-config --mcp-config "$(python3 kg_cli.py strict-mcp-config)"

The output is `mcp/claude.mcp.json` with the plugin folder and the port filled in, and the relay
under its full name. Merge its `mcpServers` into a config that names other servers. Stdlib only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import shim

PLUGIN = "codebase-kg"
RELAY = "codebase-kg-hooks"
RELAY_SERVER = f"plugin:{PLUGIN}:{RELAY}"
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DECLARED = PLUGIN_ROOT / "mcp" / "claude.mcp.json"


def _filled(value: Any, root: str, port: int) -> Any:
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${user_config.server_port}", str(port))
    if isinstance(value, dict):
        return {key: _filled(item, root, port) for key, item in value.items()}
    if isinstance(value, list):
        return [_filled(item, root, port) for item in value]
    return value


def config(port: int, root: Path = PLUGIN_ROOT, windows: bool = sys.platform == "win32") -> dict[str, Any]:
    declared = json.loads(DECLARED.read_text(encoding="utf-8"))["mcpServers"]
    servers = _filled(declared, root.as_posix(), port)
    relay = servers.pop(RELAY)
    if windows:
        # Claude Code runs the .cmd beside an extension-less path only for a plugin's own server.
        relay["command"] += ".cmd"
    servers[RELAY_SERVER] = relay
    return {"mcpServers": servers}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    port = shim.http_port()
    if args[:1] == ["--port"] and len(args) == 2 and args[1].isdigit():
        port = int(args[1])
    elif args:
        print("usage: kg_cli.py strict-mcp-config [--port N]", file=sys.stderr)
        return 2
    print(json.dumps(config(port), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

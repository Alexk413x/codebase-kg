"""Print the Codex config entry for this plugin's MCP server: kg_cli.py codex-config.

Codex starts no MCP server a plugin declares, and cannot use the HTTP entry
Claude Code uses, which needs a bearer token Codex has no way to read. This
prints a `[mcp_servers.codebase-kg]` table that runs the stdio shim in this
plugin folder. The path holds the plugin version, so run the command again
after each update. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[2] / "launch"


def launcher(platform: str = sys.platform) -> Path:
    return LAUNCH / ("kg-shim.cmd" if platform == "win32" else "kg-shim")


def entry(platform: str = sys.platform) -> str:
    return (
        "[mcp_servers.codebase-kg]\n"
        f"command = '{launcher(platform)}'\n"
        "startup_timeout_sec = 60\n"
    )


def main() -> int:
    print("# Add to ~/.codex/config.toml, then restart Codex.")
    print(entry(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

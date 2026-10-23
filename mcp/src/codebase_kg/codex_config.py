"""Print the Codex config entry for this plugin's MCP server: kg_cli.py codex-config [--port N] [--home DIR] [--stdio].

Codex starts no MCP server a plugin declares, so you paste one table into
`~/.codex/config.toml`. The table connects Codex to the shared HTTP server
directly: `url` names the server, and `http_headers_helper` runs
`mcp/launch/kg_headers.py`, which prints the request headers, the bearer token
among them, and starts the server when nothing serves the port. Codex gives
the helper no variables and expands none in its command, so every setting is a
literal argument. `--stdio` prints the older table that runs the stdio shim
(`mcp/launch/kg-shim`) instead, which needs no HTTP server. The path in both
holds the plugin version, so run the command again after each update.
Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import shim

LAUNCH = Path(__file__).resolve().parents[2] / "launch"
STARTUP_TIMEOUT = 30
TOOL_TIMEOUT = 120


def launcher(platform: str = sys.platform) -> Path:
    return LAUNCH / ("kg-shim.cmd" if platform == "win32" else "kg-shim")


def toml_string(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def helper_command(port: int, home: str | None = None, platform: str = sys.platform) -> str:
    """The `http_headers_helper` command line, which Codex splits like a Windows command line."""
    python = "py -3" if platform == "win32" else "python3"
    words = [str(LAUNCH / "kg_headers.py"), "--port", str(port)]
    if home:
        words += ["--home", home]
    words += ["--cwd", "--start"]
    return f"{python} -I -S " + " ".join(f'"{w}"' if " " in w else w for w in words)


def entry(port: int | None = None, home: str | None = None, platform: str = sys.platform) -> str:
    port = shim.http_port() if port is None else port
    return (
        "[mcp_servers.codebase-kg]\n"
        f"url = 'http://127.0.0.1:{port}/mcp'\n"
        f"http_headers_helper = {toml_string(helper_command(port, home, platform))}\n"
        f"startup_timeout_sec = {STARTUP_TIMEOUT}\n"
        f"tool_timeout_sec = {TOOL_TIMEOUT}\n"
    )


def stdio_entry(platform: str = sys.platform) -> str:
    return (
        "[mcp_servers.codebase-kg]\n"
        f"command = {toml_string(str(launcher(platform)))}\n"
        "startup_timeout_sec = 60\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    port: int | None = None
    home: str | None = None
    stdio = False
    words = iter(args)
    for word in words:
        if word == "--stdio":
            stdio = True
        elif word in ("--port", "--home"):
            value = next(words, "")
            if word == "--home":
                home = value or None
            elif value.isdigit() and int(value) <= 65535:
                port = int(value)
            else:
                print(f"codex-config: --port needs a port number, not {value!r}", file=sys.stderr)
                return 2
        else:
            print("usage: kg_cli.py codex-config [--port N] [--home DIR] [--stdio]", file=sys.stderr)
            return 2
    print("# Add to ~/.codex/config.toml, then restart Codex.")
    if stdio:
        print(stdio_entry(), end="")
    else:
        print("# For the stdio shim instead, which needs no HTTP server, run codex-config with --stdio.")
        print(entry(port, home), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

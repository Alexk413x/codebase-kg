"""`kg_cli.py strict-mcp-config`: the MCP config a `--strict-mcp-config` session of Claude Code needs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from codebase_kg import strict_config

ROOT = Path(__file__).resolve().parents[2]


def _hook_servers() -> set[str]:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    return {
        hook["server"]
        for groups in hooks.values() for group in groups for hook in group["hooks"]
        if hook["type"] == "mcp_tool"
    }


def test_the_printed_relay_has_the_name_the_hooks_address() -> None:
    assert _hook_servers() == {strict_config.RELAY_SERVER}
    assert strict_config.RELAY_SERVER in strict_config.config(47821)["mcpServers"]


def test_the_config_holds_every_declared_server_with_nothing_left_to_fill_in() -> None:
    declared = json.loads((ROOT / "mcp" / "claude.mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    servers = strict_config.config(47999)["mcpServers"]
    assert set(servers) == {strict_config.PLUGIN, strict_config.RELAY_SERVER} and len(servers) == len(declared)
    text = json.dumps(servers)
    assert "${" not in text
    assert servers[strict_config.PLUGIN]["url"] == "http://127.0.0.1:47999/mcp"
    assert servers[strict_config.RELAY_SERVER]["env"] == {"CLAUDE_PLUGIN_OPTION_SERVER_PORT": "47999"}


def test_the_relay_command_is_a_launcher_that_exists_on_each_platform() -> None:
    for windows, name in ((True, "kg-hooks.cmd"), (False, "kg-hooks")):
        command = strict_config.config(47821, windows=windows)["mcpServers"][strict_config.RELAY_SERVER]["command"]
        assert Path(command) == ROOT / "mcp" / "launch" / name and Path(command).is_file()


def test_the_cli_prints_the_config_and_takes_a_port() -> None:
    cli = [sys.executable, "-I", "-S", str(ROOT / "mcp" / "launch" / "kg_cli.py"), "strict-mcp-config"]
    done = subprocess.run([*cli, "--port", "47998"], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == strict_config.config(47998)
    bad = subprocess.run([*cli, "--port", "x"], capture_output=True, text=True, check=False)
    assert bad.returncode == 2 and "usage: kg_cli.py strict-mcp-config" in bad.stderr and bad.stdout == ""

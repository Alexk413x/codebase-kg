"""What Codex reads from the plugin folder, and that Claude Code's files stay as they were."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from codebase_kg import codex_config, tools

ROOT = Path(tools.__file__).resolve().parents[3]
RUNNER = ROOT / "mcp" / "launch" / "kg_cli.py"


def _manifest(folder: str) -> dict[str, object]:
    return json.loads((ROOT / folder / "plugin.json").read_text(encoding="utf-8"))


def test_the_codex_manifest_tracks_the_claude_manifest():
    claude, codex = _manifest(".claude-plugin"), _manifest(".codex-plugin")
    for key in ("name", "version", "description", "license"):
        assert codex[key] == claude[key], key


def test_the_codex_manifest_declares_no_mcp_server():
    # Codex follows the Claude manifest's mcpServers when no .codex-plugin exists, loads the
    # HTTP entry, and logs "invalid port number" at every start. The manifest must not name one.
    assert "mcpServers" not in _manifest(".codex-plugin")


def test_claude_names_its_mcp_file_and_the_plugin_has_no_root_mcp_json():
    path = _manifest(".claude-plugin")["mcpServers"]
    assert path == "./mcp/claude.mcp.json"
    assert (ROOT / path).is_file()
    assert not (ROOT / ".mcp.json").exists()


def test_the_entry_points_at_the_launcher_for_the_platform():
    assert codex_config.entry("win32").splitlines()[1].endswith("kg-shim.cmd'")
    assert codex_config.entry("linux").splitlines()[1].endswith("kg-shim'")
    assert codex_config.launcher("win32").is_file()
    assert codex_config.launcher("linux").is_file()


def test_codex_config_prints_a_table_for_this_checkout():
    out = subprocess.run(
        [sys.executable, str(RUNNER), "codex-config"], capture_output=True, text=True, check=True
    ).stdout
    assert "[mcp_servers.codebase-kg]" in out
    assert str(codex_config.launcher()) in out

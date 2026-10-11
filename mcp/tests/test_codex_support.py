"""What Codex reads from the plugin folder, and that Claude Code's files stay as they were."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import codex_config, tools
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

ROOT = Path(tools.__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "hooks"))

import kg_search_gate as gate

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


# --- the Codex hooks file -----------------------------------------------------
CODEX_HOOKS = ROOT / "hooks" / "codex-hooks.json"


def _codex_commands() -> list[tuple[str, dict[str, object]]]:
    doc = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    return [
        (group["matcher"], hook)
        for groups in doc["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]


def test_the_codex_manifest_names_the_codex_hooks_file():
    assert _manifest(".codex-plugin")["hooks"] == "./hooks/codex-hooks.json"
    assert CODEX_HOOKS.is_file()


def test_codex_hooks_use_no_field_codex_ignores_and_no_shell_syntax():
    for _, hook in _codex_commands():
        assert set(hook) <= {"type", "command", "commandWindows", "timeout", "async"}, hook
        for key in ("command", "commandWindows"):
            text = str(hook[key])
            assert not any(s in text for s in ("||", "&&", "/dev/null", "command -v")), text
        assert str(hook["command"]).startswith("python3 "), hook
        assert str(hook["commandWindows"]).startswith("py -3 "), hook


def test_every_codex_hook_script_exists_and_hooks_json_runs_it_too():
    claude = (ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    for _, hook in _codex_commands():
        script = str(hook["command"]).split("/hooks/")[1].rstrip('"')
        assert (ROOT / "hooks" / script).is_file(), script
        assert script in claude, script


def test_codex_matchers_name_only_tools_codex_reports():
    matchers = {matcher for matcher, _ in _codex_commands()}
    assert matchers == {
        "Bash", "mcp__.*codebase[-_]?kg.*", "apply_patch|Edit|Write", "startup|resume|clear"
    }
    # Codex has no Grep, Glob or PowerShell tool name, and ignores a per-hook `if`.
    assert not any("Grep" in m or "PowerShell" in m for m in matchers)


# --- the gates on the payloads Codex sends ------------------------------------
@pytest.fixture
def codex_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "gatestate"
    state.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(state))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)
    proj = tmp_path / "repo"
    (proj / "knowledge").mkdir(parents=True)
    (proj / "src" / "domain").mkdir(parents=True)
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="test", root="src", generated="2026-08-01"),
        [Node(id="n", kind="K", description="A thing.", anchors=[Anchor("domain/A.kt", "A")])],
    )
    return proj


def _codex(proj: Path, tool: str, tool_input: dict[str, Any], **extra: Any) -> dict[str, Any] | None:
    payload = {
        "hook_event_name": "PreToolUse", "model": "m", "permission_mode": "bypassPermissions",
        "session_id": "s", "turn_id": "t", "tool_use_id": f"u-{tool}-{len(str(tool_input))}",
        "transcript_path": "r.jsonl", "cwd": str(proj), "tool_name": tool, "tool_input": tool_input,
        **extra,
    }
    return payload


def _gate(payload: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any] | None:
    gate._run(payload)
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def _decision(result: dict[str, Any] | None) -> str | None:
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def test_a_codex_shell_search_is_denied(codex_repo: Path, capsys: pytest.CaptureFixture[str]):
    result = _gate(_codex(codex_repo, "Bash", {"command": "grep -rn Ranker src"}), capsys)
    assert _decision(result) == "deny"
    assert "committed code graph" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("server", ["codebase-kg", "codebase_kg", "codebase-kg-stdio", "codebase_kg_stdio"])
def test_a_codex_named_graph_query_earns_credit(
    server: str, codex_repo: Path, capsys: pytest.CaptureFixture[str]
):
    assert _gate(_codex(codex_repo, "mcp__" + server + "__kg_search", {"query": "x"}), capsys) is None
    search = _codex(codex_repo, "Bash", {"command": "grep -rn Ranker src"})
    assert _gate(search, capsys) is None


def test_a_codex_subagent_search_gets_advice_not_a_denial(
    codex_repo: Path, capsys: pytest.CaptureFixture[str]
):
    payload = _codex(
        codex_repo, "Bash", {"command": "grep -rn Ranker src"}, agent_id="a1", agent_type="worker"
    )
    result = _gate(payload, capsys)
    assert _decision(result) is None
    assert "committed code graph" in result["hookSpecificOutput"]["additionalContext"]


def test_a_codex_shell_command_that_is_not_a_search_passes(
    codex_repo: Path, capsys: pytest.CaptureFixture[str]
):
    assert _gate(_codex(codex_repo, "Bash", {"command": "git status --short"}), capsys) is None

"""What Codex reads from the plugin folder, and that Claude Code's files stay as they were."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

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


def _table(text: str) -> dict[str, str]:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines[0] == "[mcp_servers.codebase-kg]"
    return dict(ln.split(" = ", 1) for ln in lines[1:])


def test_the_stdio_entry_points_at_the_launcher_for_the_platform():
    assert codex_config.stdio_entry("win32").splitlines()[1].endswith("kg-shim.cmd'")
    assert codex_config.stdio_entry("linux").splitlines()[1].endswith("kg-shim'")
    assert codex_config.launcher("win32").is_file()
    assert codex_config.launcher("linux").is_file()


def test_the_entry_connects_to_the_http_server_through_the_headers_helper():
    table = _table(codex_config.entry(47821, platform="linux"))
    assert set(table) == {"url", "http_headers_helper", "startup_timeout_sec", "tool_timeout_sec"}
    assert table["url"] == "'http://127.0.0.1:47821/mcp'"
    assert table["http_headers_helper"] == (
        f"'python3 -I -S {ROOT / 'mcp' / 'launch' / 'kg_headers.py'} --port 47821 --cwd --start'"
    )
    assert int(table["startup_timeout_sec"]) >= 30 and int(table["tool_timeout_sec"]) > 60


def test_the_windows_helper_runs_through_the_py_launcher():
    helper = codex_config.helper_command(47821, platform="win32")
    assert helper.startswith("py -3 -I -S ") and helper.endswith("kg_headers.py --port 47821 --cwd --start")


def test_the_helper_takes_the_cache_folder_as_an_argument():
    assert "--home /srv/kg " in codex_config.helper_command(1, "/srv/kg", platform="linux")


def test_a_word_with_a_space_gets_double_quotes():
    helper = codex_config.helper_command(1, "/srv/a b", platform="linux")
    assert '--home "/srv/a b" --cwd' in helper and "'" not in helper


def test_a_command_with_a_single_quote_becomes_a_basic_string():
    assert codex_config.toml_string("a b") == "'a b'"
    assert codex_config.toml_string("C:\\a's") == '"C:\\\\a' + "'" + 's"'


def test_the_port_defaults_to_the_servers_port(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SERVER_PORT", raising=False)
    monkeypatch.setenv("CODEBASE_KG_PORT", "47900")
    assert "47900/mcp" in codex_config.entry() and "--port 47900 " in codex_config.entry()


def _config(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(RUNNER), "codex-config", *args], capture_output=True, text=True,
                          check=False)


def test_codex_config_prints_the_http_table_for_this_checkout():
    out = _config("--port", "47931").stdout
    table = _table(out)
    assert table["url"] == "'http://127.0.0.1:47931/mcp'"
    assert str(ROOT / "mcp" / "launch" / "kg_headers.py") in table["http_headers_helper"]
    assert "--port 47931" in table["http_headers_helper"]
    assert "command" not in table and "--stdio" in out


def test_codex_config_prints_the_stdio_table_with_stdio():
    table = _table(_config("--stdio").stdout)
    assert set(table) == {"command", "startup_timeout_sec"}
    assert str(codex_config.launcher()) in table["command"]


def test_codex_config_refuses_a_bad_port_and_an_unknown_word():
    assert _config("--port", "x").returncode == 2
    assert _config("--port", "70000").returncode == 2
    assert _config("--bogus").returncode == 2


def test_the_printed_helper_command_runs_and_prints_headers(tmp_path: Path):
    table = _table(_config("--port", "47932", "--home", str(tmp_path / "cache")).stdout)
    _, _, rest = table["http_headers_helper"].strip("'").partition(" -I -S ")
    if '"' in rest:
        pytest.skip("the checkout path has a space")
    args = [word for word in rest.split(" ") if word != "--start"]
    done = subprocess.run([sys.executable, "-I", "-S", *args], capture_output=True, text=True, cwd=tmp_path,
                          timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    headers = json.loads(done.stdout)
    assert unquote(headers["X-Codebase-KG-Cwd"]) == str(tmp_path) and "X-Codebase-KG-Client" in headers


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

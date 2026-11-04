"""Copilot adapters stay separate from the Claude and Codex entry points."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hooks"))
import kg_copilot as copilot

SETUP = ROOT / "mcp" / "launch" / "kg_copilot.py"
spec = importlib.util.spec_from_file_location("copilot_setup", SETUP)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)
    monkeypatch.setattr(copilot, "server_answers", lambda: True)
    proj = tmp_path / "repo"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "Known.py").write_text("class Known: pass\n", encoding="utf-8")
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="copilot", root="src", generated="2026-11-03"),
        [Node(id="known", kind="Class", description="Known component.",
              anchors=[Anchor("Known.py", "Known")])],
    )
    return proj


def payload(repo: Path, tool: str, args: Any, session: str = "session") -> dict[str, Any]:
    return {"cwd": str(repo), "sessionId": session, "toolName": tool, "toolArgs": args}


def run(mode: str, data: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    copilot.run(mode, data)
    text = capsys.readouterr().out.strip()
    return json.loads(text) if text else {}


def test_setup_prints_only_json_and_does_not_write_config(tmp_path: Path):
    for component in ("mcp", "hooks"):
        result = subprocess.run(
            [sys.executable, str(SETUP), component], cwd=tmp_path, capture_output=True,
            text=True, timeout=10, check=True,
        )
        assert isinstance(json.loads(result.stdout), dict)
        assert not result.stderr
    assert list(tmp_path.iterdir()) == []


def test_stdio_config_uses_direct_python_args_without_pinning_a_repo():
    entry = setup.mcp_config(sys.executable)["mcpServers"]["codebase-kg"]
    assert entry == {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", "-S", str(ROOT / "mcp" / "src" / "codebase_kg" / "shim.py")],
        "env": {}, "tools": ["*"],
    }


def test_hooks_use_native_events_and_shell_free_launches():
    config = setup.hooks_config("python with spaces")
    assert config["version"] == 1
    assert set(config["hooks"]) == {"preToolUse", "postToolUse"}
    for hooks in config["hooks"].values():
        assert hooks[0]["exec"] == "python with spaces"
        assert hooks[0]["args"][0] == str(ROOT / "hooks" / "kg_copilot.py")
        assert not {"command", "bash", "powershell"} & hooks[0].keys()


def test_copilot_manifest_does_not_point_at_claude_transport_or_hooks():
    manifest = json.loads((ROOT / ".github" / "plugin" / "plugin.json").read_text())
    assert manifest["hooks"] == "./hooks/copilot-hooks.json"
    # Copilot starts plugin-owned MCP commands in the plugin folder, not the
    # session repo. The project config printer supplies the routed server.
    assert "mcpServers" not in manifest
    assert manifest["skills"] == "./skills"
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    removal = next(line for line in release.splitlines() if "git rm " in line).split()
    assert ".github" not in removal
    assert ".github/plugin" not in removal


def test_native_search_denial_is_a_copilot_decision(repo: Path, capsys: pytest.CaptureFixture[str]):
    result = run("pre", payload(repo, "rg", {"pattern": "Something", "path": "src"}), capsys)
    assert result["permissionDecision"] == "deny"
    assert "committed code graph" in result["permissionDecisionReason"]
    assert "hookSpecificOutput" not in result
    assert run("pre", payload(repo, "rg", {"pattern": "Something", "path": "src"}), capsys) == {}


@pytest.mark.parametrize("name", [
    "codebase-kg-kg_search", "codebase-kg/kg_node", "mcp__codebase-kg__kg_find_by_path",
])
def test_query_credit_is_bounded_and_session_specific(
    repo: Path, name: str, capsys: pytest.CaptureFixture[str]
):
    assert run("pre", payload(repo, name, {}), capsys) == {}
    for index in range(3):
        assert run("pre", payload(repo, "rg", {"pattern": f"Term{index}"}), capsys) == {}
    assert run("pre", payload(repo, "glob", {"pattern": "**/*.py"}), capsys)["permissionDecision"] == "deny"
    assert run("pre", payload(repo, "rg", {"pattern": "Other"}, "other"), capsys)["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ["a11y-kg-kg_search", "codebase-kg-kg_upsert_node"])
def test_other_graphs_and_writes_do_not_earn_query_credit(
    repo: Path, tool: str, capsys: pytest.CaptureFixture[str]
):
    run("pre", payload(repo, tool, {}), capsys)
    assert run("pre", payload(repo, "rg", {"pattern": "Missing"}), capsys)["permissionDecision"] == "deny"


def test_missing_stdio_server_allows_search_with_actionable_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(copilot, "server_answers", lambda: False)
    result = run("pre", payload(repo, "rg", {"pattern": "Missing"}), capsys)
    assert "permissionDecision" not in result
    assert "stdio server is unavailable" in result["additionalContext"]


def test_copilot_repo_wins_over_leftover_claude_environment(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "other"))
    assert run("pre", payload(repo, "rg", {"pattern": "Missing"}), capsys)["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool,key", [
    ("apply_patch", "input"), ("Edit", "input"), ("apply_patch", "command")
])
def test_patch_payload_nudges_in_session_repo(
    repo: Path, tool: str, key: str, capsys: pytest.CaptureFixture[str]
):
    patch = "*** Begin Patch\n*** Add File: src/Added.py\n+pass\n*** End Patch"
    result = run("post", payload(repo, tool, {key: patch}), capsys)
    assert "Added.py" in result["additionalContext"]
    assert "systemMessage" not in result


def test_patch_paths_include_deletions_and_both_sides_of_moves(repo: Path):
    patch = "*** Delete File: src/Deleted.py\n*** Update File: src/A.py\n*** Move to: src/B.py\n"
    result = copilot.edited_paths(copilot.normalize(payload(repo, "apply_patch", {"input": patch})))
    assert result == [str(repo / "src" / name) for name in ("Deleted.py", "A.py", "B.py")]


def test_relative_edit_paths_and_json_string_args(repo: Path, capsys: pytest.CaptureFixture[str]):
    result = run("post", payload(repo, "edit", json.dumps({"path": "src/Added.py"})), capsys)
    assert "Added.py" in result["additionalContext"]


def test_powershell_keeps_its_identity_for_push_and_search_parsing(repo: Path):
    normalized = copilot.normalize(payload(repo, "powershell", {"command": "git push"}))
    assert normalized["tool_name"] == "PowerShell"


def test_native_push_denial_is_translated(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(copilot.push, "_repo_root", lambda _cwd: repo)
    monkeypatch.setattr(copilot.push, "stale_files", lambda _repo: (["src/Known.py"], "knowledge/code_graph.db"))
    result = run("pre", payload(repo, "powershell", {"command": "git push"}), capsys)
    assert result["permissionDecision"] == "deny"
    assert "Known.py" in result["permissionDecisionReason"]
    assert "hookSpecificOutput" not in result


def test_freeform_patch_input_is_adapted(repo: Path, capsys: pytest.CaptureFixture[str]):
    patch = "*** Begin Patch\n*** Add File: src/Added.py\n+pass\n*** End Patch"
    result = run("post", payload(repo, "apply_patch", patch), capsys)
    assert "Added.py" in result["additionalContext"]


def test_generated_config_routes_two_repos_concurrently(tmp_path: Path):
    from test_shared_server import _client, _finish, _kill_daemon, _node_ids, _repo

    cache = tmp_path / "cache"
    repos = [_repo(tmp_path / "alpha space", "alpha"), _repo(tmp_path / "beta space", "beta")]
    entry = setup.mcp_config(sys.executable)["mcpServers"]["codebase-kg"]
    processes = []
    try:
        for repo in repos:
            processes.append(subprocess.Popen(
                [entry["command"], *entry["args"]], cwd=repo,
                env={**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache),
                     "CODEBASE_KG_PATH": "", "CODEBASE_KG_MAX_WORKERS": "0"},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        clients = [_client(proc) for proc in processes]
        for client in clients:
            client.initialize()
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(_node_ids, clients)) == [{"alpha_widget"}, {"beta_widget"}]
    finally:
        for proc in processes:
            _finish(proc)
        _kill_daemon(cache)


def test_probe_refuses_missing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path / "cache"))
    assert copilot.server_answers() is False


def test_probe_authenticates_the_stdio_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_shared_server import _client, _finish, _kill_daemon, _repo

    cache = tmp_path / "cache"
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(cache))
    repo = _repo(tmp_path / "repo", "alpha")
    entry = setup.mcp_config(sys.executable)["mcpServers"]["codebase-kg"]
    proc = subprocess.Popen(
        [entry["command"], *entry["args"]], cwd=repo,
        env={**os.environ, "CODEBASE_KG_PATH": "", "CODEBASE_KG_MAX_WORKERS": "0"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _client(proc).initialize()
        assert copilot.server_answers() is True
        shim = copilot.load_shim()
        state = shim.read_state(shim.server_build())
        assert state
        state["token"] = "wrong-token"
        monkeypatch.setattr(shim, "read_state", lambda _build: state)
        monkeypatch.setattr(copilot, "load_shim", lambda: shim)
        assert copilot.server_answers() is False
    finally:
        _finish(proc)
        _kill_daemon(cache)

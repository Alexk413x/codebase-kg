"""The Antigravity adapter turns agy hook payloads into the shared gates' input and back."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hooks"))
import kg_antigravity as agy

ADAPTER = ROOT / "hooks" / "kg_antigravity.py"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agy.copilot, "server_answers", lambda: True)
    proj = tmp_path / "repo"
    (proj / "src").mkdir(parents=True)
    (proj / "docs").mkdir()
    (proj / "src" / "Known.py").write_text("class Known: pass\n", encoding="utf-8")
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="agy", root="src", generated="2026-11-26"),
        [Node(id="known", kind="Class", description="Known component.",
              anchors=[Anchor("Known.py", "Known")])],
    )
    return proj


def pre(repo: Path, name: str, args: dict[str, Any], conversation: str = "conversation") -> dict[str, Any]:
    return {
        "hookEventName": "PreToolUse", "cwd": str(repo), "conversationId": conversation,
        "toolCall": {"id": "call_1", "name": name, "args": args},
    }


def shell(repo: Path, command: str, conversation: str = "conversation", **extra: Any) -> dict[str, Any]:
    return pre(repo, "run_command", {"CommandLine": command, **extra}, conversation)


def mcp(repo: Path, server: str, tool: str, conversation: str = "conversation") -> dict[str, Any]:
    return pre(repo, "call_mcp_tool", {"ServerName": server, "ToolName": tool, "Arguments": {}}, conversation)


def run(data: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    agy.run(data)
    text = capsys.readouterr().out.strip()
    return json.loads(text) if text else {}


def test_a_gated_search_is_denied_in_agy_form_and_a_repeat_passes(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    result = run(shell(repo, "rg Something src"), capsys)
    assert result["decision"] == "deny"
    assert "committed code graph" in result["reason"]
    assert set(result) == {"decision", "reason"}
    assert run(shell(repo, "rg Something src"), capsys) == {}


@pytest.mark.parametrize("server", ["codebase-kg", "codebase-kg_codebase-kg", "Codebase_KG"])
def test_a_graph_query_earns_credit_for_that_conversation_only(
    repo: Path, server: str, capsys: pytest.CaptureFixture[str]
):
    assert run(mcp(repo, server, "kg_search"), capsys) == {}
    for index in range(3):
        assert run(shell(repo, f"rg Term{index} src"), capsys) == {}
    assert run(shell(repo, "rg Last src"), capsys)["decision"] == "deny"
    assert run(shell(repo, "rg Other src", "other"), capsys)["decision"] == "deny"


@pytest.mark.parametrize("server, tool", [
    ("a11y-kg", "kg_search"), ("codebase-kg", "kg_upsert_node"), ("probe", "kg_search"),
])
def test_other_graphs_and_writes_do_not_earn_credit(
    repo: Path, server: str, tool: str, capsys: pytest.CaptureFixture[str]
):
    assert run(mcp(repo, server, tool), capsys) == {}
    assert run(shell(repo, "rg Missing src"), capsys)["decision"] == "deny"


def test_a_stale_push_is_denied_with_the_stale_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(agy.push, "_repo_root", lambda _cwd: repo)
    monkeypatch.setattr(agy.push, "stale_files", lambda _repo: (["src/Known.py"], "knowledge/code_graph.db"))
    result = run(shell(repo, "git push"), capsys)
    assert result["decision"] == "deny"
    assert "Known.py" in result["reason"]


def test_a_push_passes_when_nothing_is_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(agy.push, "_repo_root", lambda _cwd: repo)
    monkeypatch.setattr(agy.push, "stale_files", lambda _repo: ([], "knowledge/code_graph.db"))
    assert run(shell(repo, "git push"), capsys) == {}


@pytest.mark.parametrize("data", [
    {"name": "run_command", "args": {"CommandLine": "git status"}},
    {"name": "run_command", "args": {"CommandLine": "python -m pytest -q"}},
    {"name": "run_command", "args": {"CommandLine": "Get-Content src/Known.py"}},
    {"name": "read_file", "args": {"Path": "src/Known.py"}},
    {"name": "write_file", "args": {"Path": "src/New.py"}},
    {"name": "call_mcp_tool", "args": {"ServerName": "other", "ToolName": "search", "Arguments": {}}},
])
def test_unrelated_calls_pass_through(
    repo: Path, data: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    call = {"id": "call_1", **data}
    payload = {"hookEventName": "PreToolUse", "cwd": str(repo), "conversationId": "c", "toolCall": call}
    assert run(payload, capsys) == {}


def test_a_cd_in_the_command_moves_the_scope_out_of_the_graph(repo: Path, capsys: pytest.CaptureFixture[str]):
    assert run(shell(repo, "cd docs && rg Something ."), capsys) == {}
    assert run(shell(repo, "rg Something ."), capsys)["decision"] == "deny"


def test_a_session_in_a_subfolder_still_finds_the_graph(repo: Path, capsys: pytest.CaptureFixture[str]):
    assert run(shell(repo / "src", "rg Something"), capsys)["decision"] == "deny"


def test_the_agent_working_folder_wins_over_a_leftover_claude_environment(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "other"))
    assert run(shell(repo, "rg Missing src"), capsys)["decision"] == "deny"


def test_an_unavailable_server_allows_the_search_without_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(agy.copilot, "server_answers", lambda: False)
    assert run(shell(repo, "rg Missing src"), capsys) == {}


def test_shell_calls_take_the_dialect_of_the_platform(repo: Path):
    normalized = agy.normalize(shell(repo, "git push"))
    assert normalized["tool_name"] == agy.SHELL
    assert normalized["tool_input"] == {"command": "git push"}
    assert normalized["session_id"] == "conversation"


def test_the_session_notice_becomes_an_injected_system_message(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(agy.session_start, "advice", lambda _proj: "codebase-kg: wire this clone")
    result = run({"hookEventName": "SessionStart", "cwd": str(repo), "conversationId": "c"}, capsys)
    assert result == {"injectSteps": [{"systemMessage": {"systemMessage": "codebase-kg: wire this clone"}}]}


def test_a_session_with_no_notice_prints_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(agy.session_start, "advice", lambda _proj: None)
    assert run({"hookEventName": "SessionStart", "cwd": str(repo), "conversationId": "c"}, capsys) == {}


def test_a_crash_in_the_adapter_answers_allow(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def fail(*_args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(agy, "pre_tool", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(shell(repo, "rg Something src"))))
    agy.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("stdin", [
    "", "not json", "[]", "null", "{}", '{"hookEventName": "PreToolUse"}',
    '{"hookEventName": "PreToolUse", "toolCall": "text"}',
    '{"hookEventName": "PreToolUse", "toolCall": {"name": "run_command", "args": []}}',
    '{"hookEventName": "PreToolUse", "cwd": 7, "toolCall": {"name": "run_command", "args": {"CommandLine": 7}}}',
    '{"hookEventName": "SessionStart", "cwd": []}',
])
def test_malformed_input_exits_zero_and_prints_nothing(stdin: str, tmp_path: Path):
    """agy blocks the tool call when a PreToolUse hook exits non-zero or prints text that is not JSON."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(ADAPTER)], input=stdin, cwd=tmp_path,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_the_script_runs_under_isolated_python_with_a_real_payload(repo: Path):
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(ADAPTER)], input=json.dumps(shell(repo, "git status")),
        cwd=repo / "docs", capture_output=True, text=True, timeout=30, check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _hooks() -> dict[str, Any]:
    return json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))


def _handlers(config: dict[str, Any]) -> list[dict[str, Any]]:
    handlers: list[dict[str, Any]] = []
    for events in config.values():
        for groups in events.values():
            for group in groups:
                handlers.extend(group.get("hooks", [group]))
    return handlers


def test_the_hooks_file_holds_only_command_handlers():
    """One handler type agy refuses (mcp_tool, http, agent) makes it skip the whole file."""
    handlers = _handlers(_hooks())
    assert handlers
    assert {handler["type"] for handler in handlers} == {"command"}


def test_the_hooks_file_uses_the_named_hook_shape_at_the_plugin_root():
    config = _hooks()
    assert list(config) == ["codebase-kg"]
    events = config["codebase-kg"]
    assert set(events) == {"SessionStart", "PreToolUse"}
    assert all("matcher" not in handler for handler in events["SessionStart"])
    (group,) = events["PreToolUse"]
    assert group["matcher"] == "run_command|call_mcp_tool"


def test_every_command_tries_each_interpreter_and_then_allows():
    """cmd.exe and sh both read `||`. A hook that exits non-zero blocks the tool call, so a
    machine with no working Python must still end with exit 0. python3 is a Store stub on
    Windows, so the py launcher comes first."""
    for handler in _handlers(_hooks()):
        steps = [step.strip() for step in handler["command"].split("||")]
        assert [step.split()[0] for step in steps] == ["py", "python3", "python", "exit"]
        assert steps[-1] == "exit 0"
        for step in steps[:-1]:
            assert step.endswith(" -I -S ./hooks/kg_antigravity.py")
        assert isinstance(handler["timeout"], int)
    assert (ROOT / "hooks" / "kg_antigravity.py").is_file()


def test_the_manifest_is_native_and_the_release_keeps_both_files():
    manifest = json.loads((ROOT / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "codebase-kg"
    assert "hooks" not in manifest, "agy reads hooks.json at the plugin root and ignores a manifest path"
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    removal = next(line for line in release.splitlines() if "git rm " in line).split()
    assert ".antigravity-plugin" not in removal
    assert "hooks.json" not in removal

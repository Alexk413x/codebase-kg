"""What the server's push gate does with a skip: it asks the user, and an agent cannot answer for them.

`test_push_gate.py` runs detection and the plain deny against the hook script and against this endpoint.
This file covers the server's own part: a push that carries `SKIP_KG` or a matching `KG_STALE_ACK`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import classic, core, push_gate
from test_push_gate import KNOWN, RANKER, _make_repo

CAPS = push_gate.CAPABILITIES_KEY
WITH_FORM = {"_meta": {CAPS: {"elicitation": {"form": {}}, "roots": {}}}}
WITHOUT = {"_meta": {CAPS: {"roots": {}}}}


def _ask(repo: Path, command: str, params: dict[str, Any] | None = None, tool: str = "Bash") -> dict[str, Any]:
    return push_gate.call({"tool_name": tool, "command": command, "cwd": str(repo)}, params)


def _hook(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def _decision(result: dict[str, Any]) -> tuple[str, str]:
    specific = _hook(result)["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    return specific["permissionDecision"], specific["permissionDecisionReason"]


def _answered(answer: dict[str, Any]) -> dict[str, Any]:
    return {**WITH_FORM, "inputResponses": {push_gate.ANSWER: answer}}


@pytest.fixture
def stale(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, drift=(KNOWN, RANKER))


def test_a_stale_push_with_no_skip_is_denied_and_tells_the_agent_to_refresh_without_asking(stale: Path) -> None:
    decision, reason = _decision(_ask(stale, "git push origin main", WITH_FORM))
    assert decision == "deny"
    assert "/codebase-kg:refresh" in reason and "without asking the user" in reason
    assert f"src/{KNOWN}" in reason and f"src/{RANKER}" in reason and "KG_STALE_ACK=2" in reason
    assert "Only the user can accept this drift" in reason


@pytest.mark.parametrize("command", [
    "SKIP_KG=1 git push", "KG_STALE_ACK=2 git push origin main", "export SKIP_KG=1 && git push",
    "git add -A && KG_STALE_ACK=2 git push --no-verify",
])
def test_a_skip_in_the_command_asks_the_user_through_an_elicitation(stale: Path, command: str) -> None:
    result = _ask(stale, command, WITH_FORM)

    assert result["resultType"] == "input_required" and "content" not in result
    (key, request), = result["inputRequests"].items()
    assert key == push_gate.ANSWER and request["method"] == "elicitation/create"
    form = request["params"]
    assert form["mode"] == "form"
    assert "2 mapped file(s)" in form["message"] and f"src/{KNOWN}" in form["message"]
    assert form["requestedSchema"] == {
        "type": "object", "required": ["approve"],
        "properties": {"approve": {"type": "boolean", "title": "Push without updating the graph", "default": False}},
    }, "one boolean and no optional text: Codex does not submit a form with an empty optional field"


def test_the_users_approval_lets_the_push_through(stale: Path) -> None:
    answer = {"action": "accept", "content": {"approve": True}}
    assert _hook(_ask(stale, "SKIP_KG=1 git push", _answered(answer))) == {}


@pytest.mark.parametrize("answer", [
    {"action": "accept", "content": {"approve": False}},
    {"action": "accept", "content": {}},
    {"action": "accept"},
    {"action": "accept", "content": {"approve": "true"}},
    {"action": "decline"},
    {"action": "cancel"},
    {},
    "yes",
])
def test_anything_but_a_ticked_accept_refuses_the_push(stale: Path, answer: Any) -> None:
    """A headless run answers `cancel`, and an untouched form answers `approve: false`."""
    decision, reason = _decision(_ask(stale, "SKIP_KG=1 git push", _answered(answer)))
    assert decision == "deny"
    assert "did not approve" in reason and "/codebase-kg:refresh" in reason and "Do not retry the skip" in reason


@pytest.mark.parametrize("params", [WITHOUT, {}, None, {"_meta": {"progressToken": 1}}])
def test_a_client_without_elicitation_gets_the_question_in_its_permission_prompt(
    stale: Path, params: dict[str, Any] | None,
) -> None:
    decision, reason = _decision(_ask(stale, "KG_STALE_ACK=2 git push", params))
    assert decision == "ask"
    assert "Push without updating the code graph?" in reason and f"src/{RANKER}" in reason


def test_an_ack_that_names_the_wrong_count_is_no_skip(stale: Path) -> None:
    decision, _ = _decision(_ask(stale, "KG_STALE_ACK=7 git push", WITH_FORM))
    assert decision == "deny"


def test_one_push_without_a_skip_is_denied_before_anyone_is_asked(stale: Path) -> None:
    decision, _ = _decision(_ask(stale, "SKIP_KG=1 git push; git push", WITH_FORM))
    assert decision == "deny"
    approved = _answered({"action": "accept", "content": {"approve": True}})
    assert _decision(_ask(stale, "SKIP_KG=1 git push; git push", approved))[0] == "deny"


def test_the_servers_own_environment_grants_nothing(stale: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKIP_KG", "1")
    monkeypatch.setenv("KG_STALE_ACK", "2")
    assert _decision(_ask(stale, "git push", WITH_FORM))[0] == "deny"


def test_a_current_graph_is_never_asked_about(tmp_path: Path) -> None:
    clean = _make_repo(tmp_path)
    assert _hook(_ask(clean, "SKIP_KG=1 git push", WITH_FORM)) == {}
    assert _hook(_ask(clean, "git push", WITH_FORM)) == {}


def test_powershell_commands_are_read_too(stale: Path) -> None:
    result = _ask(stale, "$env:SKIP_KG = '1'; git push", WITH_FORM, tool="PowerShell")
    assert result["resultType"] == "input_required"
    assert _decision(_ask(stale, "git push", WITH_FORM, tool="PowerShell"))[0] == "deny"


@pytest.mark.parametrize("args", [None, "text", {}, {"tool_name": "Bash", "command": "git push", "cwd": "relative"},
                                  {"tool_name": "Edit", "command": "git push", "cwd": "C:/x"},
                                  {"tool_name": "Bash", "command": "git push 'unclosed", "cwd": "C:/x"}])
def test_a_call_the_gate_cannot_read_answers_an_empty_object(args: Any) -> None:
    assert _hook(push_gate.call(args, WITH_FORM)) == {}


def test_a_failing_gate_answers_an_empty_object(stale: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(push_gate, "stale_files", boom)
    assert _hook(_ask(stale, "git push", WITH_FORM)) == {}


def test_the_graph_write_tools_get_an_allow(stale: Path) -> None:
    out = _hook(push_gate.allow_write())
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_both_endpoints_are_unlisted_and_reachable_on_every_path(stale: Path) -> None:
    assert push_gate.TOOL not in core.TOOLS and push_gate.ALLOW_WRITE_TOOL not in core.TOOLS
    args = {"tool_name": "Bash", "command": "SKIP_KG=1 git push", "cwd": str(stale)}
    asked = core.hook_result(push_gate.TOOL, args, "client", {"name": push_gate.TOOL, "arguments": args, **WITH_FORM})
    assert asked is not None and asked["resultType"] == "input_required"
    older = classic.answer(core.Core(None), None, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": push_gate.TOOL, "arguments": args}})
    assert older is not None and _decision(older["result"])[0] == "ask", "no channel for an elicitation there"
    allowed = core.hook_result(push_gate.ALLOW_WRITE_TOOL, {}, "client", {})
    assert allowed is not None and _hook(allowed)["hookSpecificOutput"]["permissionDecision"] == "allow"

"""What the server's push gate does with a skip: it hands the decision to the user.

`test_push_gate.py` runs detection and the plain deny against the hook script and against this endpoint.
This file covers the server's own part: a push that carries `SKIP_KG` or a matching `KG_STALE_ACK` is
answered with `ask`, never with an allow, so an agent cannot skip the check on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import classic, core, push_gate
from test_push_gate import KNOWN, RANKER, _make_repo


def _ask(repo: Path, command: str, tool: str = "Bash") -> dict[str, Any]:
    return push_gate.call({"tool_name": tool, "command": command, "cwd": str(repo)})


def _hook(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False and "resultType" not in result
    return json.loads(result["content"][0]["text"])


def _decision(result: dict[str, Any]) -> tuple[str, str]:
    specific = _hook(result)["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    return specific["permissionDecision"], specific["permissionDecisionReason"]


@pytest.fixture
def stale(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, drift=(KNOWN, RANKER))


def test_a_stale_push_with_no_skip_is_denied_and_tells_the_agent_to_refresh_without_asking(stale: Path) -> None:
    decision, reason = _decision(_ask(stale, "git push origin main"))
    assert decision == "deny"
    assert "/codebase-kg:refresh" in reason and "without asking the user" in reason
    assert f"src/{KNOWN}" in reason and f"src/{RANKER}" in reason and "KG_STALE_ACK=2" in reason
    assert "Only the user can accept this drift" in reason


@pytest.mark.parametrize("command", [
    "SKIP_KG=1 git push", "KG_STALE_ACK=2 git push origin main", "export SKIP_KG=1 && git push",
    "git add -A && KG_STALE_ACK=2 git push --no-verify",
])
def test_a_skip_in_the_command_is_put_to_the_user_and_never_allowed_outright(stale: Path, command: str) -> None:
    """`ask` makes the client show its permission prompt. A run with no user has none and is denied, and
    the prompt has no timeout, where a hook that waits on a dialog is cancelled and the command runs."""
    decision, reason = _decision(_ask(stale, command))
    assert decision == "ask"
    assert "Push without updating the code graph?" in reason
    assert "2 mapped file(s)" in reason and f"src/{KNOWN}" in reason and f"src/{RANKER}" in reason


def test_the_gate_never_answers_allow_and_never_waits_on_the_client(stale: Path) -> None:
    for command in ("git push", "SKIP_KG=1 git push", "KG_STALE_ACK=2 git push", "KG_STALE_ACK=9 git push"):
        result = _ask(stale, command)
        assert "inputRequests" not in result
        assert _decision(result)[0] in ("deny", "ask")


def test_an_ack_that_names_the_wrong_count_is_no_skip(stale: Path) -> None:
    assert _decision(_ask(stale, "KG_STALE_ACK=7 git push"))[0] == "deny"


@pytest.mark.parametrize("ack", ["\u00b2", "\u0662", "2\u00b2", "1" * 40, "+2", "2.0"])
def test_an_ack_that_is_not_plain_digits_is_no_skip_and_cannot_break_the_gate(stale: Path, ack: str) -> None:
    """`str.isdigit` accepts a superscript two and `int` rejects it. An error must not open the gate."""
    assert _decision(_ask(stale, f"KG_STALE_ACK={ack} git push"))[0] == "deny"


def test_one_push_without_a_skip_is_denied_before_anyone_is_asked(stale: Path) -> None:
    assert _decision(_ask(stale, "SKIP_KG=1 git push; git push"))[0] == "deny"
    assert _decision(_ask(stale, "git push && KG_STALE_ACK=2 git push"))[0] == "deny"


def test_a_skip_across_two_repositories_shows_the_user_both(tmp_path: Path) -> None:
    one = _make_repo(tmp_path / "one", drift=(KNOWN,))
    two = _make_repo(tmp_path / "two", drift=(KNOWN, RANKER))
    command = f"SKIP_KG=1 git -C {one.as_posix()} push; SKIP_KG=1 git -C {two.as_posix()} push"

    decision, reason = _decision(_ask(tmp_path, command))

    assert decision == "ask" and "Push 2 repositories" in reason
    assert "1 mapped file(s)" in reason and "2 mapped file(s)" in reason
    assert reason.count(f"src/{KNOWN}") == 2 and f"src/{RANKER}" in reason
    assert str(one) in reason and str(two) in reason


def test_the_servers_own_environment_grants_nothing(stale: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKIP_KG", "1")
    monkeypatch.setenv("KG_STALE_ACK", "2")
    assert _decision(_ask(stale, "git push"))[0] == "deny"


def test_a_current_graph_is_never_asked_about(tmp_path: Path) -> None:
    clean = _make_repo(tmp_path)
    assert _hook(_ask(clean, "SKIP_KG=1 git push")) == {}
    assert _hook(_ask(clean, "git push")) == {}


def test_powershell_commands_are_read_too(stale: Path) -> None:
    assert _decision(_ask(stale, "$env:SKIP_KG = '1'; git push", tool="PowerShell"))[0] == "ask"
    assert _decision(_ask(stale, "git push", tool="PowerShell"))[0] == "deny"


@pytest.mark.parametrize("args", [None, "text", {}, {"tool_name": "Bash", "command": "git push", "cwd": "relative"},
                                  {"tool_name": "Edit", "command": "git push", "cwd": "C:/x"},
                                  {"tool_name": "Bash", "command": "git push 'unclosed", "cwd": "C:/x"}])
def test_a_call_the_gate_cannot_read_answers_an_empty_object(args: Any) -> None:
    assert _hook(push_gate.call(args)) == {}


def test_a_failure_before_any_stale_push_is_found_answers_an_empty_object(
    stale: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(repo: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(push_gate, "stale_files", boom)
    assert _hook(_ask(stale, "git push")) == {}


def test_a_failure_after_a_stale_push_was_found_denies(stale: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command's text is the agent's. A value in it that makes the gate raise must not open it."""
    def boom(script: Any, env: dict[str, str], count: int) -> bool:
        raise ValueError("boom")

    monkeypatch.setattr(push_gate, "_wants_skip", boom)
    decision, reason = _decision(_ask(stale, "SKIP_KG=1 git push"))
    assert decision == "deny" and f"src/{KNOWN}" in reason


def test_the_graph_write_tools_get_an_allow() -> None:
    out = _hook(push_gate.allow_write())
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_both_endpoints_are_unlisted_and_reachable_on_every_path(stale: Path) -> None:
    assert push_gate.TOOL not in core.TOOLS and push_gate.ALLOW_WRITE_TOOL not in core.TOOLS
    args = {"tool_name": "Bash", "command": "SKIP_KG=1 git push", "cwd": str(stale)}
    direct = core.hook_result(push_gate.TOOL, args, "client")
    assert direct is not None and _decision(direct)[0] == "ask"
    older = classic.answer(core.Core(None), None, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": push_gate.TOOL, "arguments": args}})
    assert older is not None and _decision(older["result"])[0] == "ask"
    allowed = core.hook_result(push_gate.ALLOW_WRITE_TOOL, {}, "client")
    assert allowed is not None and _hook(allowed)["hookSpecificOutput"]["permissionDecision"] == "allow"


def _codex(repo: Path, command: str) -> dict[str, Any]:
    return push_gate.call({"tool_name": "Bash", "command": command, "cwd": str(repo), "prompts": False})


@pytest.mark.parametrize("command", ["git push", "SKIP_KG=1 git push", "KG_STALE_ACK=2 git push origin main"])
def test_a_client_that_cannot_prompt_gets_a_deny_for_a_skip_too(stale: Path, command: str) -> None:
    """Codex does not honor `ask`: it runs the command. There a skip prefix is denied, and the text does
    not offer one, or the agent would retry it for nothing."""
    decision, reason = _decision(_codex(stale, command))
    assert decision == "deny"
    assert "KG_STALE_ACK" not in reason and "SKIP_KG" not in reason
    assert "/codebase-kg:refresh" in reason and "run git push themselves" in reason


@pytest.mark.parametrize("value", [False, "false", "False", 0, "0"])
def test_no_prompts_is_understood_as_text_too(stale: Path, value: Any) -> None:
    """A hook template may hand the flag over as text. Read as true it would make the gate answer
    `ask`, which Codex does not honor: the push would run."""
    out = push_gate.call({"tool_name": "Bash", "command": "KG_STALE_ACK=2 git push", "cwd": str(stale), "prompts": value})
    assert _decision(out)[0] == "deny"


@pytest.mark.parametrize("value", [True, "true", None, 1])
def test_any_other_value_keeps_the_prompt(stale: Path, value: Any) -> None:
    args = {"tool_name": "Bash", "command": "KG_STALE_ACK=1 git push", "cwd": str(stale)}
    if value is not None:
        args["prompts"] = value
    assert _decision(push_gate.call(args))[0] in ("ask", "deny")
    assert push_gate._prompts(args) is True


def test_a_client_that_cannot_prompt_passes_a_current_graph(tmp_path: Path) -> None:
    assert _hook(_codex(_make_repo(tmp_path), "SKIP_KG=1 git push")) == {}


def test_a_failure_after_a_stale_push_denies_without_the_prefix_for_such_a_client(
    stale: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = push_gate._pre

    def fails_once(decision: str, reason: str) -> dict[str, Any]:
        calls.append(decision)
        if len(calls) == 1:
            raise ValueError("boom")
        return original(decision, reason)

    monkeypatch.setattr(push_gate, "_pre", fails_once)
    decision, reason = _decision(_codex(stale, "git push"))
    assert calls == ["deny", "deny"] and decision == "deny" and "KG_STALE_ACK" not in reason

"""Adapt Antigravity CLI hooks to the shared gates without changing other clients."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_copilot as copilot
import kg_push_gate as push
import kg_session_start as session_start
from _config import run_hook

SHELL = "PowerShell" if os.name == "nt" else "Bash"


def graph_query(args: dict[str, Any]) -> bool:
    server = str(args.get("ServerName") or "").lower().replace("_", "-")
    return (
        (server == "codebase-kg" or server.endswith("-codebase-kg"))
        and args.get("ToolName") in copilot.QUERIES
    )


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    call = data.get("toolCall")
    call = call if isinstance(call, dict) else {}
    args = call.get("args")
    args = args if isinstance(args, dict) else {}
    tool = str(call.get("name") or "")
    tool_input: dict[str, Any] = {}
    if tool == "run_command":
        tool = SHELL
        tool_input["command"] = args.get("CommandLine")
    elif tool == "call_mcp_tool" and graph_query(args):
        tool = "mcp__codebase-kg__kg_search"
    return {
        **data, "tool_name": tool, "tool_input": tool_input,
        "session_id": data.get("conversationId") or "-",
    }


def pre_tool(payload: dict[str, Any], answer: io.StringIO) -> None:
    tool = payload["tool_name"]
    if tool == "mcp__codebase-kg__kg_search":
        copilot.search_gate(payload)
    elif tool == SHELL:
        copilot.search_gate(payload)
        if not answer.getvalue():
            push._run(payload)


def session_notice(payload: dict[str, Any]) -> None:
    real_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        session_start.main()
    finally:
        sys.stdin = real_stdin


def run(data: dict[str, Any]) -> None:
    # The hook process starts in the plugin folder, and the server handshake sends its folder.
    cwd = data.get("cwd")
    if isinstance(cwd, str) and os.path.isdir(cwd):
        os.chdir(cwd)
    # A leftover Claude environment must not route an Antigravity session to another repo.
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
    event = data.get("hookEventName")
    payload = normalize(data)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        if event == "PreToolUse":
            pre_tool(payload, captured)
        elif event == "SessionStart":
            session_notice(payload)
    text = captured.getvalue().strip()
    if not text:
        return
    result = json.loads(text)
    if event == "SessionStart":
        message = result.get("systemMessage")
        if message:
            print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": message}}]}))
        return
    specific = result.get("hookSpecificOutput", {})
    decision = specific.get("permissionDecision")
    if decision in {"deny", "ask"}:
        print(json.dumps({"decision": decision, "reason": specific.get("permissionDecisionReason", "")}))


def main() -> None:
    run_hook(run)


if __name__ == "__main__":
    main()

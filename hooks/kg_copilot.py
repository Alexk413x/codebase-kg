"""Adapt Copilot CLI hooks to the shared gates without changing other clients."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_post_edit_check as edits
import kg_push_gate as push
import kg_search_gate as gate
from _config import run_hook
from _shim_loader import load_shim

TOOLS = {
    "bash": "Bash", "powershell": "PowerShell", "grep": "Grep", "rg": "Grep",
    "glob": "Glob", "edit": "Edit", "str_replace_editor": "Edit",
    "create": "Write", "apply_patch": "Edit",
}
QUERIES = {
    "kg_search", "kg_node", "kg_neighborhood", "kg_find_by_path",
    "kg_find_by_kind", "kg_find_by_link", "kg_find_by_reference",
}
PATCH_FILE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE
)


def server_answers() -> bool:
    """Check the authenticated stdio listener, not an optional HTTP port."""
    shim = load_shim()
    build = shim.server_build()
    state = shim.read_state(build)
    if not state or not shim.pid_alive(state["pid"]):
        return False
    try:
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=0.5) as conn:
            conn.sendall(json.dumps({
                "version": build, "token": state["token"], "cwd": os.getcwd(),
            }).encode() + b"\n")
            with conn.makefile("rb") as stream:
                reply = json.loads(stream.readline(65536))
                return isinstance(reply, dict) and reply.get("ok") is True
    except (OSError, ValueError):
        return False


def search_gate(payload: dict[str, Any]) -> None:
    original = gate.server_answers
    gate.server_answers = server_answers
    try:
        gate._run(payload)
    finally:
        gate.server_answers = original


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    tool = str(data.get("toolName") or data.get("tool_name") or "")
    raw = data.get("toolArgs", data.get("tool_input", {}))
    if isinstance(raw, str):
        if tool in {"apply_patch", "Edit"} and raw.lstrip().startswith("*** Begin Patch"):
            raw = {"input": raw}
        else:
            raw = json.loads(raw)
    args = dict(raw) if isinstance(raw, dict) else {}
    if "paths" in args and "path" not in args:
        paths = args["paths"]
        if isinstance(paths, str):
            args["path"] = paths
        # Multiple paths must not accidentally exempt the rest of the search.
    return {
        **data, "tool_name": TOOLS.get(tool, tool), "tool_input": args,
        "session_id": data.get("sessionId") or data.get("session_id") or "-",
    }


def graph_query(tool: str) -> bool:
    return any(tool in {
        f"codebase-kg-{name}", f"codebase-kg/{name}",
        f"mcp__codebase-kg__{name}", f"mcp__plugin_codebase-kg_codebase-kg__{name}",
    } for name in QUERIES)


def edited_paths(data: dict[str, Any]) -> list[str]:
    args = data["tool_input"]
    if data["tool_name"] not in {"Edit", "Write"}:
        return []
    path = args.get("file_path") or args.get("path")
    if isinstance(path, str) and path:
        paths = [path]
    else:
        patch = args.get("input") or args.get("command")
        paths = PATCH_FILE.findall(patch) if isinstance(patch, str) else []
    cwd = data.get("cwd") or os.getcwd()
    return list(dict.fromkeys(str(Path(cwd) / path) for path in paths))


def run(mode: str, data: dict[str, Any]) -> None:
    # A leftover Claude environment must not route a Copilot session to another repo.
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
    payload = normalize(data)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        if mode == "pre":
            tool = payload["tool_name"]
            if graph_query(tool):
                payload["tool_name"] = "mcp__codebase-kg__kg_search"
                search_gate(payload)
            elif not gate._KG_TOOL.match(tool):
                search_gate(payload)
                if not captured.getvalue():
                    push._run(payload)
        else:
            for path in edited_paths(payload):
                edits._run({**payload, "tool_name": "Edit", "tool_input": {"file_path": path}})
                if captured.getvalue():
                    break
    text = captured.getvalue().strip()
    if not text:
        return
    result = json.loads(text)
    specific = result.get("hookSpecificOutput", {})
    output = {key: specific[key] for key in ("permissionDecision", "permissionDecisionReason")
              if key in specific}
    context = specific.get("additionalContext") or result.get("systemMessage")
    if context:
        if gate.SERVER_DOWN_MESSAGE.split("{port}")[0] in context:
            context = (
                "codebase-kg: the authenticated stdio server is unavailable. "
                "This search is allowed. Reconnect codebase-kg from /mcp or restart the session."
            )
        output["additionalContext"] = context
    print(json.dumps(output))


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"pre", "post"}:
        sys.exit("usage: kg_copilot.py pre|post")
    run_hook(lambda data: run(sys.argv[1], data))


if __name__ == "__main__":
    main()

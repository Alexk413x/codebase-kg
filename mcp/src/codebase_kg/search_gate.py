"""The search gate, answered by the server for a direct hook. Stdlib only.

Claude Code calls `kg_hook_search_gate` from `PreToolUse` hooks of type
`mcp_tool` on Grep, Glob, Bash and PowerShell. The tool is not in the catalog.
It returns the hook's JSON as text: a deny with the instruction to query the
graph, context for a subagent, or `{}`. Any failure answers `{}`, so a search is
never blocked by a fault here.

The decision is `hooks/kg_search_gate.py`, loaded from the plugin folder and not
copied, so Claude Code, Codex and Copilot CLI apply one rule. That script was
written to run once per hook call as its own process. Here it runs in the
server, so this module replaces the parts that assumed a process of its own:

- Session state (credit, the denied searches) lives in this server's memory,
  keyed by project and session. A server restart clears it.
- The script's output is collected and returned, not printed.
- There is one hook entry per tool, so the claim files that stopped several
  matching handlers from acting twice are not used.
- The environment belongs to the server, not to a session. `SKIP_KG` and
  `CLAUDE_PROJECT_DIR` are removed from it, and the project is found from the
  session folder the hook sends. Nothing else in the server reads either. So
  `SKIP_KG` does not reach this gate, in the environment or written in front of
  a command: turning the gate off is the user's choice, made with `search_gate`
  in the per-clone file. An agent's own way through is to repeat the search.

The script reads module state while it decides, so calls run one at a time.

A graph query earns search credit. The server receives the queries itself, so no
hook reports them: `note_query` records the client that made one, and the gate
call that follows from the same client applies the credit to its session. A hook
call carries the session id and the client id; a model's call carries only the
client id.

When the server started after the session did, searches made before were not
checked and the credit count is gone. The first gate call of such a session, in a
repo with a graph, says so once to the user and the agent. The session's start is
the transcript's creation time where the platform records one, else the time on
the transcript's first line; with neither, nothing is said.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import shim

logger = logging.getLogger("codebase_kg.search_gate")

TOOL = "kg_hook_search_gate"
SCRIPT = shim.MCP_DIR.parent / "hooks" / "kg_search_gate.py"
STARTED = time.time()
MAX_SESSIONS = 512
MAX_CLIENTS = 4096
RESTART_SLACK = 60.0
RESTARTED = (
    "codebase-kg: its server started after this session did. Searches made before that were not "
    "checked against the code graph, and the count of searches a graph query clears starts again."
)

_lock = threading.Lock()
_script: Any = None
_unusable = False
_states: OrderedDict[tuple[str, str], dict[str, object]] = OrderedDict()
_queried: OrderedDict[str, None] = OrderedDict()
_sessions: OrderedDict[str, None] = OrderedDict()
_out: list[dict[str, Any]] = []


def _key(proj: Path, session: str) -> tuple[str, str]:
    return os.path.normcase(str(proj)), session


def _read_state(proj: Path, session: str) -> dict[str, object]:
    return dict(_states.get(_key(proj, session), {}))


def _write_state(proj: Path, session: str, state: dict[str, object]) -> bool:
    key = _key(proj, session)
    _states[key] = dict(state)
    _states.move_to_end(key)
    while len(_states) > MAX_SESSIONS:
        _states.popitem(last=False)
    return True


def _collect(text: str) -> None:
    _out.append(json.loads(text))


def _deny(reason: str) -> None:
    _out.append({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


class Unusable(Exception):
    """The gate script could not be loaded. Logged once; every later call is silent."""


def _load() -> Any:
    """The gate script as a module of this server, with its process-bound parts replaced."""
    global _unusable
    if _unusable:
        raise Unusable
    try:
        return _loaded()
    except Exception as exc:
        _unusable = True
        logger.exception("the search gate is off for this server: %s could not be loaded", SCRIPT)
        raise Unusable from exc


def _loaded() -> Any:
    global _script
    if _script is None:
        for name in ("SKIP_KG", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(name, None)
        spec = importlib.util.spec_from_file_location("codebase_kg_search_gate_script", SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {SCRIPT}")
        module = importlib.util.module_from_spec(spec)
        keep = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = keep
        replaced: dict[str, Any] = {
            "_read_state": _read_state, "_write_state": _write_state, "_claim_call": lambda tool_use_id: True,
            "server_answers": lambda: True, "skip_kg": lambda env=None: False, "emit_deny": _deny,
            "print": _collect,
        }
        for name, value in replaced.items():
            if name != "print" and not hasattr(module, name):
                raise ImportError(f"{SCRIPT.name} has no {name}; the server's gate cannot replace it")
            setattr(module, name, value)
        _script = module
    return _script


def note_query(client: str | None) -> None:
    """Record that `client` made a graph query, for the gate call that follows from it."""
    if not client:
        return
    with _lock:
        _queried[client] = None
        _queried.move_to_end(client)
        while len(_queried) > MAX_CLIENTS:
            _queried.popitem(last=False)


def _tool_input(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _created(path: str) -> float | None:
    """When the file was created, or None on a platform that does not record it.

    `st_ctime` is the creation time on Windows only. Elsewhere it is the last
    change of the file's metadata, which moves with every line appended.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    born = getattr(stat, "st_birthtime", None)
    if born:
        return float(born)
    return stat.st_ctime if sys.platform == "win32" else None


def _first_line_time(path: str) -> float | None:
    """The `timestamp` of the transcript's first line, which does not move as the file grows."""
    try:
        with open(path, encoding="utf-8") as handle:
            stamp = json.loads(handle.readline()).get("timestamp")
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def session_started(transcript: Any) -> float | None:
    if not isinstance(transcript, str) or not transcript:
        return None
    created = _created(transcript)
    return created if created is not None else _first_line_time(transcript)


def _session_predates_server(transcript: Any) -> bool:
    started = session_started(transcript)
    return started is not None and started < STARTED - RESTART_SLACK


def decide(args: dict[str, Any], client: str | None = None) -> dict[str, Any]:
    """The hook's JSON for one search-shaped tool call: a deny, context, or `{}`."""
    tool_input = _tool_input(args.get("tool_input"))
    data: dict[str, object] = {
        "tool_name": args.get("tool_name"), "tool_input": tool_input, "cwd": args.get("cwd"),
        "session_id": args.get("session_id") or "-",
    }
    if args.get("agent_id"):
        data["agent_id"] = args["agent_id"]
    session = str(data["session_id"])
    with _lock:
        script = _load()
        cwd = data["cwd"] if isinstance(data["cwd"], str) else None
        proj = script.project_dir(cwd)
        cfg = script.load_config(proj)
        has_graph = script.find_graph(proj, cfg) is not None
        if client and _queried.pop(client, 0) is None and has_graph:
            script._grant_credit(proj, session, cfg)
        first = session not in _sessions
        _sessions[session] = None
        while len(_sessions) > MAX_CLIENTS:
            _sessions.popitem(last=False)
        del _out[:]
        script._run(data)
        out: dict[str, Any] = dict(_out[0]) if _out else {}
        del _out[:]
    if first and has_graph and _session_predates_server(args.get("transcript_path")):
        out["systemMessage"] = RESTARTED
        specific = dict(out.get("hookSpecificOutput") or {"hookEventName": "PreToolUse"})
        if "permissionDecision" not in specific:
            specific["additionalContext"] = (specific.get("additionalContext", "") + "\n\n" + RESTARTED).strip()
        out["hookSpecificOutput"] = specific
    return out


def call(args: Any, client: str | None = None) -> dict[str, Any]:
    """The tool result: the hook's JSON as text. `{}` when the gate has nothing to say or failed."""
    out: dict[str, Any] = {}
    try:
        if isinstance(args, dict):
            out = decide(args, client)
    except Unusable:
        pass
    except Exception:
        logger.exception("search gate failed")
    return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}

"""PreToolUse gate — the code graph answers "where does this live?" before grep does.

The graph is a committed map of the codebase, so orienting with it is both
cheaper and more complete than a text search: it knows the components a name
does not appear in. But nothing made an agent reach for it first, so it sat
unused while `Grep` re-derived the map every session.

This gate closes that. On the first search-shaped tool call of a session it
returns a `deny` carrying the instruction to query the graph, then **stands
down for the rest of the session** — whether or not the agent complied. One
interruption per session, never a loop, and never a search that cannot
eventually run. Any codebase-kg MCP call also stands it down, so an agent that
already started at the graph never sees it at all.

It no-ops when the repo has no graph, when `SKIP_KG` is set, and when the
search is scoped outside the graph's `root`.

State (per project + session) lives in the OS temp dir — nothing is written into
the user's repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (  # noqa: E402
    IGNORE_DIRS,
    find_graph,
    graph_meta,
    load_config,
    project_dir,
)

_SEARCH_TOOLS = {"Grep", "Glob"}
_SHELL_TOOLS = {"Bash", "PowerShell"}

# Shell commands that are a codebase search by another name. Deliberately narrow:
# a false positive here denies an unrelated command, which is worse than missing
# one. `find` and `Get-ChildItem` only count when they carry a name/path filter,
# otherwise an ordinary `find . -type d` trips the gate.
_SHELL_SEARCH = re.compile(
    r"(?:^|[|;&]\s*)(?:sudo\s+)?(?:grep|egrep|fgrep|rg|ripgrep|ag|ack|fd|sls|select-string)\b"
    r"|(?:^|[|;&]\s*)(?:sudo\s+)?find\b[^|;&]*\s-(?:i?name|i?path|i?regex)\b"
    r"|(?:^|[|;&]\s*)(?:get-childitem|gci)\b[^|;&]*\s-r(?:ecurse)?\b",
    re.IGNORECASE,
)

# The plugin's own MCP tools, under either name the host gives the server
# (`mcp__codebase-kg__*` standalone, `mcp__plugin_codebase-kg_codebase-kg__*`
# when loaded as a plugin).
_KG_TOOL = re.compile(r"^mcp__.*codebase[-_]?kg.*__", re.IGNORECASE)

_STATE_TTL = 7 * 24 * 3600  # prune abandoned session files after a week

GATE_MESSAGE = (
    "codebase-kg: this repo has a committed code graph ({graph}). Query it before "
    "searching source.\n\n"
    "1. kg_search for what you are looking for — it finds the components, not just "
    "the string. Then kg_node / kg_neighborhood for anchors and relationships.\n"
    "2. Read the anchored files to confirm current behavior. The graph is "
    "authoritative for WHERE code lives; the source is authoritative for what it "
    "does now.\n"
    "The kg-query skill (/codebase-kg:query) is this workflow in full.\n\n"
    "If the graph does not cover what you need, run this search again — the gate "
    "stands down for the rest of this session either way."
)


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _state_path(proj: Path, session: str) -> Path:
    key = f"{_norm(proj)}\0{session}".encode("utf-8")
    d = Path(tempfile.gettempdir()) / "codebase-kg-gate"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{hashlib.sha1(key).hexdigest()[:16]}.json"


def _prune(directory: Path) -> None:
    """Drop state from sessions that ended long ago. Best-effort: the gate is not
    worth a failure, and a leftover file only costs a few bytes."""
    cutoff = time.time() - _STATE_TTL
    try:
        for f in directory.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def _stood_down(proj: Path, session: str) -> bool:
    try:
        data = json.loads(_state_path(proj, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("done")) if isinstance(data, dict) else False


def _stand_down(proj: Path, session: str) -> None:
    try:
        path = _state_path(proj, session)
        path.write_text(json.dumps({"done": True}), encoding="utf-8")
        _prune(path.parent)
    except OSError:
        pass


def gate_mode(cfg: dict[str, object]) -> str:
    """`search_gate` as one of block / warn / off.

    The frontmatter parser coerces `off`/`no`/`false` to a bool before this sees
    it, so a plain `search_gate: off` arrives as `False` — it has to mean off
    here, or the setting reads as an unknown string and silently stays on.
    """
    raw = cfg.get("search_gate", "block")
    if raw is False:
        return "off"
    if raw is True:
        return "block"
    mode = str(raw).strip().lower()
    return mode if mode in {"block", "warn", "off"} else "block"


def is_shell_search(command: str) -> bool:
    return bool(_SHELL_SEARCH.search(command))


def searches_mapped_code(
    tool_input: dict[str, object], proj: Path, root: str
) -> bool:
    """Is this search aimed at the code the graph describes?

    A search explicitly scoped somewhere else — docs, node_modules, a peer
    checkout — is not something the graph can answer, so gating it would only
    cost a round trip. An unscoped search is assumed to be aimed at the code,
    which is the case worth gating.
    """
    raw = tool_input.get("path") or ""
    if not isinstance(raw, str) or not raw.strip():
        return True  # unscoped → the whole repo → mapped code is in range
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = proj / raw
    try:
        rel = candidate.resolve().relative_to(proj)
    except (ValueError, OSError):
        return False  # outside the project entirely
    parts = {p.lower() for p in rel.parts}
    if parts & {d.lower() for d in IGNORE_DIRS}:
        return False
    if not root:
        return True
    scope = rel.as_posix().strip("/")
    if not scope or scope == ".":
        return True
    # Either the search sits inside root, or it is a parent directory that
    # still contains root. Both reach mapped code.
    return (scope + "/").startswith(root + "/") or (root + "/").startswith(scope + "/")


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _warn(message: str) -> None:
    print(json.dumps({"systemMessage": message, "suppressOutput": False}))


def _is_search_call(
    tool: str, tool_input: dict[str, object], cfg: dict[str, object]
) -> bool:
    if tool in _SEARCH_TOOLS:
        return True
    if tool in _SHELL_TOOLS and cfg.get("gate_shell_search", True):
        command = tool_input.get("command")
        return isinstance(command, str) and is_shell_search(command)
    return False


def _run(data: dict[str, object]) -> None:
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    proj = project_dir(data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    session = str(data.get("session_id") or "-")

    # Any graph query stands the gate down: this agent already started where it
    # should. Recorded on intent (PreToolUse), so a query that errors still counts.
    if _KG_TOOL.match(tool):
        _stand_down(proj, session)
        return

    cfg = load_config(proj)
    if not _is_search_call(tool, tool_input, cfg):
        return

    if gate_mode(cfg) == "off" or os.environ.get("SKIP_KG"):
        return

    graph = find_graph(proj, cfg)
    if graph is None:
        return  # no graph in this repo → nothing to consult

    if _stood_down(proj, session):
        return

    raw_root = str(cfg.get("root") or graph_meta(graph, "root") or "")
    root = raw_root.strip().replace("\\", "/").strip("/")
    root = "" if root == "." else root
    if not searches_mapped_code(tool_input, proj, root):
        return

    # Spend the session's one interruption before emitting, not after: a failure
    # between the message and the write would re-gate the next search and turn
    # one nudge into a loop.
    _stand_down(proj, session)
    message = GATE_MESSAGE.format(graph=graph.name)
    if gate_mode(cfg) == "warn":
        _warn(message)
    else:
        _deny(message)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            _run(data)
    except Exception:
        # Fail open. A gate that errors must let the search through, never
        # strand the agent.
        return


if __name__ == "__main__":
    main()

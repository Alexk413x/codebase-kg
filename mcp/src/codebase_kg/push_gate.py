"""The push gate, answered by the server for a direct hook. Stdlib only.

Claude Code calls `kg_hook_push_gate` from `PreToolUse` hooks of type `mcp_tool`
on Bash and PowerShell commands that run git. The tool is not in the catalog. It
denies an agent's `git push` while a mapped file is stale against `HEAD`, on the
rule the pre-push git hook uses (`git-hooks/kg_pre_push.py`).

Finding the pushes in a command is `hooks/kg_push_gate.py`, loaded from the
plugin folder, so every client reads a command the same way. What happens to a
stale push is decided here:

- **No skip in the command.** The push is denied. The reason tells the agent to
  run `/codebase-kg:refresh`, commit the graph and push again, without asking
  the user.
- **A skip in the command** (`SKIP_KG=<value>`, or `KG_STALE_ACK=<n>` naming the
  stale count, written in front of the push). An agent cannot grant itself the
  skip: the server asks the user. On MCP 2026-07-28 it answers `input_required`
  with a form elicitation that lists the stale files, and the client repeats the
  hook's call with the user's answer. The push runs only when the user accepts
  and ticks the box. A decline, a cancel, or a run with no user refuses it.
  A client that declared no elicitation gets `permissionDecision: "ask"`, which
  puts the same question in the client's own permission prompt.

The server's environment belongs to whoever started it, so `SKIP_KG` and
`KG_STALE_ACK` there mean nothing: only the command's own prefix counts.

`kg_hook_allow_write` answers `allow` for the six graph write tools. A graph
update changes no code, the graph file is in git, and the refresh the deny asks
for must not stop at a permission prompt.

Any failure answers `{}`. The git pre-push hook still blocks a stale push.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from . import commit_check, shim

logger = logging.getLogger("codebase_kg.push_gate")

TOOL = "kg_hook_push_gate"
ALLOW_WRITE_TOOL = "kg_hook_allow_write"
SCRIPT = shim.MCP_DIR.parent / "hooks" / "kg_push_gate.py"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
ANSWER = "codebase-kg-push-skip"
LIST_CAP = 20
ALLOW_WRITE = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "allow",
    "permissionDecisionReason": "codebase-kg: a graph update changes no code, and the graph file is in git.",
}}

_load_lock = threading.Lock()
_script: Any = None


def _load() -> Any:
    global _script
    with _load_lock:
        if _script is None:
            spec = importlib.util.spec_from_file_location("codebase_kg_push_gate_script", SCRIPT)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {SCRIPT}")
            module = importlib.util.module_from_spec(spec)
            keep = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec.loader.exec_module(module)
            finally:
                sys.dont_write_bytecode = keep
            _script = module
    return _script


def stale_files(repo: Path) -> tuple[list[str], str] | None:
    """Repo-relative stale mapped files and the graph path, or None when there is no readable graph."""
    with commit_check._lock:
        checker = commit_check._load_checker()
        checker.GIT_CWD = str(repo)
        try:
            cfg = checker.load_config(repo)
            graph_rel = checker.find_graph_rel(repo, cfg)
            graph = checker.read_graph(repo / graph_rel) if graph_rel else None
            if graph_rel is None or graph is None:
                return None
            root = checker.norm_root(cfg.get("root") or graph.root)
            split = checker.repo_staleness(graph, root, ["HEAD"])
            return [checker._root_to_rel(p, root) for p in split.stale], graph_rel
        finally:
            checker.GIT_CWD = None


def _listed(stale: list[str]) -> str:
    shown = "\n".join(f"  ~ {p}" for p in stale[:LIST_CAP])
    return shown + (f"\n  ... and {len(stale) - LIST_CAP} more" if len(stale) > LIST_CAP else "")


def deny_reason(stale: list[str], graph_rel: str) -> str:
    return (
        f"codebase-kg: push blocked. {len(stale)} mapped file(s) no longer match {graph_rel}:\n"
        f"{_listed(stale)}\n\n"
        "Refresh the graph, then push again. Do this without asking the user:\n"
        "1. Run /codebase-kg:refresh.\n"
        f"2. Commit {graph_rel}.\n"
        "3. Run git push again.\n\n"
        f"Only the user can accept this drift. If they asked you to push without the update, prefix the push: "
        f"KG_STALE_ACK={len(stale)} git push ... and they are asked to approve it. "
        "--no-verify does not skip this check."
    )


def refused_reason(stale: list[str], graph_rel: str) -> str:
    return (
        f"codebase-kg: push blocked. The user did not approve a push with {len(stale)} stale mapped file(s) "
        f"against {graph_rel}:\n{_listed(stale)}\n\n"
        "Run /codebase-kg:refresh, commit the graph and run git push again. Do not retry the skip."
    )


def question(stale: list[str], graph_rel: str) -> str:
    return (
        f"Push without updating the code graph? {len(stale)} mapped file(s) no longer match {graph_rel}:\n"
        f"{_listed(stale)}"
    )


def _form(stale: list[str], graph_rel: str) -> dict[str, Any]:
    return {"method": "elicitation/create", "params": {
        "mode": "form",
        "message": question(stale, graph_rel),
        "requestedSchema": {
            "type": "object",
            "properties": {"approve": {
                "type": "boolean", "title": "Push without updating the graph", "default": False,
            }},
            "required": ["approve"],
        },
    }}


def _wants_skip(script: Any, env: dict[str, str], count: int) -> bool:
    """True when the command's own prefix carries `SKIP_KG`, or a `KG_STALE_ACK` that names `count`."""
    ack = (env.get("KG_STALE_ACK") or "").strip()
    return bool(script.skip_kg(env)) or (ack.isdigit() and int(ack) == count)


def _approved(answer: Any) -> bool:
    content = answer.get("content") if isinstance(answer, dict) else None
    return isinstance(answer, dict) and answer.get("action") == "accept" and isinstance(content, dict) \
        and content.get("approve") is True


def _pre(decision: str, reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}


def decide(args: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The hook's JSON for one shell command, or an `input_required` result that asks the user."""
    tool, command, cwd = args.get("tool_name"), args.get("command"), args.get("cwd")
    if tool not in ("Bash", "PowerShell") or not isinstance(command, str) or not isinstance(cwd, str):
        return {}
    if "git" not in command or "push" not in command or not Path(cwd).is_absolute():
        return {}
    script = _load()
    try:
        pushes = script.find_pushes(script.powershell_to_shell(command) if tool == "PowerShell" else command, Path(cwd))
    except ValueError:
        return {}
    fields: dict[str, Any] = params if isinstance(params, dict) else {}
    responses = fields.get("inputResponses")
    meta = fields.get("_meta")
    capabilities = meta.get(CAPABILITIES_KEY) if isinstance(meta, dict) else None
    checked: dict[Path, tuple[list[str], str] | None] = {}
    skips: list[tuple[list[str], str]] = []
    for push in pushes:
        if push.deletes_only:
            continue
        repo = script._repo_root(push.cwd)
        if repo is None:
            continue
        if repo not in checked:
            checked[repo] = stale_files(repo)
        found = checked[repo]
        if found is None or not found[0]:
            continue
        if not _wants_skip(script, push.env, len(found[0])):
            return _pre("deny", deny_reason(*found))
        skips.append(found)
    # A push with no skip is denied above before anyone is asked: one command can hold both.
    if not skips:
        return {}
    stale, graph_rel = skips[0]
    if isinstance(responses, dict) and ANSWER in responses:
        return {} if _approved(responses[ANSWER]) else _pre("deny", refused_reason(stale, graph_rel))
    if isinstance(capabilities, dict) and "elicitation" in capabilities:
        return {"resultType": "input_required", "inputRequests": {ANSWER: _form(stale, graph_rel)}}
    return _pre("ask", question(stale, graph_rel))


def _as_text(out: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}


def call(args: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The tool result: the hook's JSON as text, or the `input_required` result itself."""
    out: dict[str, Any] = {}
    try:
        if isinstance(args, dict):
            out = decide(args, params)
    except Exception:
        logger.exception("push gate failed")
    return out if out.get("resultType") == "input_required" else _as_text(out)


def allow_write() -> dict[str, Any]:
    return _as_text(ALLOW_WRITE)

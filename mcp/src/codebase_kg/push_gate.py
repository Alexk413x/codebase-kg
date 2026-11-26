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
  skip: the answer is `permissionDecision: "ask"`, with the stale files in the
  reason, so the client puts the question to the user in its permission prompt.
  A run with no user has no prompt and the client denies the call.

The question is not an MCP elicitation, although a hook's call can raise one.
The client keeps the hook waiting while an elicitation is open, and when the
hook's timeout passes it cancels the hook and runs the command: an unanswered
dialog became an approved push. `ask` ends the hook at once, and the permission
prompt that follows has no timeout. It also prompts when the session runs with
permissions bypassed.

The server's environment belongs to whoever started it, so `SKIP_KG` and
`KG_STALE_ACK` there mean nothing: only the command's own prefix counts.

`kg_hook_allow_write` answers `allow` for the six graph write tools. A graph
update changes no code, the graph file is in git, and the refresh the deny asks
for must not stop at a permission prompt.

A failure before any stale push is found answers `{}`, and the git pre-push hook
still blocks. A failure after one is found answers deny: the command's text is
the agent's, and a value in it that makes this code raise must not open the gate.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any

from . import commit_check, shim

logger = logging.getLogger("codebase_kg.push_gate")

TOOL = "kg_hook_push_gate"
ALLOW_WRITE_TOOL = "kg_hook_allow_write"
SCRIPT = shim.MCP_DIR.parent / "hooks" / "kg_push_gate.py"
_COUNT = re.compile(r"[0-9]{1,9}")
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


def deny_reason(stale: list[str], graph_rel: str, prompts: bool = True) -> str:
    """What the agent is told. `prompts` is False for a client that shows no permission prompt: there the
    skip prefix would only be denied again, so the text does not offer it."""
    way_out = (
        f"If they asked you to push without the update, prefix the push: "
        f"KG_STALE_ACK={len(stale)} git push ... and they are asked to approve it. "
        if prompts else
        "This client cannot ask them, so no prefix skips the check here. If they want to push without the "
        "update, tell them to run git push themselves in a terminal. "
    )
    return (
        f"codebase-kg: push blocked. {len(stale)} mapped file(s) no longer match {graph_rel}:\n"
        f"{_listed(stale)}\n\n"
        "Refresh the graph, then push again. Do this without asking the user:\n"
        "1. Run /codebase-kg:refresh.\n"
        f"2. Commit {graph_rel}.\n"
        "3. Run git push again.\n\n"
        f"Only the user can accept this drift. {way_out}"
        "--no-verify does not skip this check."
    )


Found = tuple[Path, list[str], str]


def question(skips: list[Found]) -> str:
    """What the user is asked. Every repository the command would push with a stale graph is listed, so
    one answer never covers files the user was not shown."""
    if len(skips) == 1:
        _, stale, graph_rel = skips[0]
        return (
            f"Push without updating the code graph? {len(stale)} mapped file(s) no longer match {graph_rel}:\n"
            f"{_listed(stale)}"
        )
    blocks = [f"{repo}: {len(stale)} mapped file(s) no longer match {graph_rel}:\n{_listed(stale)}"
              for repo, stale, graph_rel in skips]
    return f"Push {len(skips)} repositories without updating their code graphs?\n\n" + "\n\n".join(blocks)


def _wants_skip(script: Any, env: dict[str, str], count: int) -> bool:
    """True when the command's own prefix carries `SKIP_KG`, or a `KG_STALE_ACK` that names `count`."""
    ack = (env.get("KG_STALE_ACK") or "").strip()
    return bool(script.skip_kg(env)) or (_COUNT.fullmatch(ack) is not None and int(ack) == count)


def _pre(decision: str, reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}


def _prompts(args: Any) -> bool:
    """False for a client that said it shows no permission prompt. A hook template can deliver the
    value as text, and reading `"false"` as true would turn a deny into an `ask` that such a client runs."""
    value = args.get("prompts") if isinstance(args, dict) else None
    return not (value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in ("false", "0", "no")))


def decide(args: dict[str, Any], seen: list[Found] | None = None) -> dict[str, Any]:
    """The hook's JSON for one shell command: `{}`, a deny, or an ask.

    `prompts: false` in the arguments names a client that does not honor `ask` (Codex runs the command):
    there a skip is a deny. Each stale push found is appended to `seen`, so the caller knows one exists
    if this raises.
    """
    tool, command, cwd = args.get("tool_name"), args.get("command"), args.get("cwd")
    prompts = _prompts(args)
    if tool not in ("Bash", "PowerShell") or not isinstance(command, str) or not isinstance(cwd, str):
        return {}
    if "git" not in command or "push" not in command or not Path(cwd).is_absolute():
        return {}
    script = _load()
    try:
        pushes = script.find_pushes(script.powershell_to_shell(command) if tool == "PowerShell" else command, Path(cwd))
    except ValueError:
        return {}
    checked: dict[Path, tuple[list[str], str] | None] = {}
    skips: list[Found] = []
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
        if seen is not None:
            seen.append((repo, *found))
        if not prompts or not _wants_skip(script, push.env, len(found[0])):
            return _pre("deny", deny_reason(*found, prompts))
        if not any(repo == listed for listed, _, _ in skips):
            skips.append((repo, *found))
    # A push with no skip is denied above before anyone is asked: one command can hold both.
    return _pre("ask", question(skips)) if skips else {}


def _as_text(out: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}


def call(args: Any) -> dict[str, Any]:
    """The tool result: the hook's JSON as text."""
    out: dict[str, Any] = {}
    seen: list[Found] = []
    try:
        if isinstance(args, dict):
            out = decide(args, seen)
    except Exception:
        logger.exception("push gate failed")
        if seen:
            _, stale, graph_rel = seen[0]
            out = _pre("deny", deny_reason(stale, graph_rel, _prompts(args)))
    return _as_text(out)


def allow_write() -> dict[str, Any]:
    return _as_text(ALLOW_WRITE)

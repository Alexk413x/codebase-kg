"""PreToolUse gate — the code graph answers "where does this live?" before grep does.

The graph is a committed map of the codebase, so orienting with it is both
cheaper and more complete than a text search: it knows the components a name
does not appear in. But nothing made an agent reach for it first, so it sat
unused while `Grep` re-derived the map every session.

This gate closes that. A search-shaped tool call is denied with the instruction
to query the graph — and it keeps doing that rather than standing down for the
session after one nudge. One nudge was too little: the agent paid it once,
learned nothing, and grepped freely for the rest of the turn.

## The three ways through, and why none of them is a flag

A `PreToolUse` hook cannot add an argument to `Grep`; it sees the call the agent
already made and answers allow or deny. So an override has to be inferred from
what the agent DID, which is the better design anyway — a self-declared
`force=true` is a rubber stamp an agent learns to always pass.

  a query buys credit    a codebase-kg MCP call clears the next `gate_credit`
                         searches (default 3, settable per repo). The allowance
                         is for what the answer did NOT name — a partial answer
                         leaves a remainder only searching will find.
  a located search       a search that names its files is never gated, anchored
                         or not, existing yet or not. The agent has evidently
                         found the file; gating it would only cost a round trip.
  repeat to insist       a search for the same thing as one already denied —
                         the same call, or the same pattern however the
                         command around it is reworded — is allowed, for the
                         rest of the session. This is the escape hatch for code
                         the graph does not cover yet, and it is what makes the
                         gate unable to strand anyone.

That last one is load-bearing. A gate that can refuse the same call forever is
worse than no gate, so the retry always passes — the cost of insisting is one
round trip, not an argument with a hook.

It no-ops when the repo has no graph, when `SKIP_KG` is set, and when the
search is scoped outside the code the graph maps: beside `root`, outside a
declared `covers` or inside `exempt`, or in a directory the graph anchors
nothing beneath (scratch folders, docs, a gitignored build output).

State (per project + session) lives in the OS temp dir — nothing is written into
the user's repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (
    as_int,
    as_local_path,
    emit_deny,
    find_graph,
    graph_meta,
    load_config,
    norm_path,
    norm_root,
    parse_patterns,
    project_dir,
    read_state,
    run_hook,
    skip_kg,
    state_dir,
    state_path,
    strip_heredocs,
    write_state,
)
from _shell_search import (
    _SEARCH_WORDS,
    REASONS,
    Verdict,
    in_non_source_bundle,
    is_shell_search,
    only_non_source,
    other_clauses,
    parse_shell_search,
    shell_search_is_gated,
    shell_search_verdict,
    targets_verdict,
)
from _shim_loader import load_shim

__all__ = [
    "_SEARCH_WORDS",
    "Verdict",
    "shell_search_is_gated",
    "strip_heredocs",
]

_SEARCH_TOOLS = {"Grep", "Glob"}
_SHELL_TOOLS = {"Bash", "PowerShell"}

# The plugin's own MCP tools, under either name the host gives the server
# (`mcp__codebase-kg__*` standalone, `mcp__plugin_codebase-kg_codebase-kg__*`
# when loaded as a plugin).
_KG_TOOL = re.compile(r"^mcp__.*codebase[-_]?kg.*__", re.IGNORECASE)

_STATE_TTL = 7 * 24 * 3600  # prune abandoned session files after a week

GATE_MESSAGE = (
    "codebase-kg: denied a search of {target}: {reason}. This repo has a committed "
    "code graph ({graph}). Query it before searching source.\n\n"
    "1. kg_search from the codebase-kg server (its tool name contains codebase-kg) for "
    "what you are looking for — it finds the components, not just the string. Then "
    "codebase-kg's kg_node / kg_neighborhood for anchors and relationships. The kg_search "
    "of a11y-kg or a driver searches another graph and clears nothing here.\n"
    "2. Read the anchored files to confirm current behavior. The graph is "
    "authoritative for WHERE code lives; the source is authoritative for what it "
    "does now.\n"
    "The query skill (/codebase-kg:query) is this workflow in full.\n\n"
    "A graph query clears the next {credit} search(es). A search that names its "
    "files is never gated, and neither is one of a folder the graph maps no code in, "
    "or one that can only match assets or project settings. And if the graph does "
    "not cover what you "
    "need, search for the same pattern again — a repeat is always allowed, "
    "however you rephrase the command."
)


_STATE_DIR = "codebase-kg-gate"


def _state_path(proj: Path, session: str) -> Path:
    return state_path(_STATE_DIR, f"{norm_path(proj)}\0{session}")


def _prune(directory: Path) -> None:
    """Drop state from sessions that ended long ago. Best-effort: the gate is not
    worth a failure, and a leftover file only costs a few bytes."""
    cutoff = time.time() - _STATE_TTL
    try:
        for f in [*directory.glob("*.json"), *directory.glob(".*.tmp"), *directory.glob("calls/*")]:
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def _claim_call(tool_use_id: str) -> bool:
    """True for the first handler to see this tool call, False for the rest.

    `hooks.json` registers one handler per search word, so a command such as
    `grep a f && rg b d` starts two gate processes in parallel. Only one may
    spend credit or record a denial.
    """
    try:
        d = state_dir(_STATE_DIR) / "calls"
        d.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(tool_use_id.encode("utf-8")).hexdigest()[:16]
        os.close(os.open(d / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _read_state(proj: Path, session: str) -> dict[str, object]:
    return read_state(_state_path(proj, session))


def _write_state(proj: Path, session: str, state: dict[str, object]) -> bool:
    """Persist the gate's bookkeeping. False when it could not be written.

    The caller has to know, because the escape hatch lives in this file: if the
    denial cannot be recorded, the repeat cannot be recognised, and a `deny`
    would then refuse the same search forever. The gate degrades to `warn`
    rather than take that risk.
    """
    path = _state_path(proj, session)
    if not write_state(path, state):
        return False
    _prune(path.parent)
    return True


def search_key(tool: str, tool_input: dict[str, object]) -> str:
    """A stable identity for one search, so an immediate repeat is recognisable.

    Only the fields that decide WHAT is searched: a different `head_limit` or
    `output_mode` on the same pattern is the same question asked again, and
    treating it as a new one would deny an agent that merely widened its own
    result window.
    """
    parts = [tool, str(tool_input.get("pattern") or ""), str(tool_input.get("path") or ""),
             str(tool_input.get("glob") or ""), str(tool_input.get("command") or "")]
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def subject_key(tool: str, tool_input: dict[str, object], proj: Path) -> str | None:
    """A stable identity for WHAT a search looks for, however it is phrased.

    An agent that insists rarely repeats itself byte for byte: it adds
    `-maxdepth 4`, widens the path, drops a `|| echo`, or swaps `find` for
    `Glob`. Each rewording read as a new question and drew a fresh denial, so
    the escape hatch never opened and the agent circled. The terms searched for
    — a grep pattern, a `find -name` value, a Glob pattern — are what stays
    fixed, so a repeat of those is the agent insisting.
    """
    if tool in _SHELL_TOOLS:
        command = tool_input.get("command")
        parsed = parse_shell_search(command, proj) if isinstance(command, str) else None
        terms = sorted(set(parsed.terms)) if parsed else []
    else:
        terms = [str(tool_input.get("pattern") or "")]
    terms = [t for t in terms if t]
    if not terms:
        return None
    return "s:" + hashlib.sha1("\0".join(terms).encode("utf-8")).hexdigest()[:16]


_DENIED_MEMORY = 16  # recent denials remembered, so parallel searches cannot evict each other


def _denied_list(state: dict[str, object]) -> list[str]:
    raw = state.get("denied")
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str)]
    return []


def gate_credit(cfg: dict[str, object]) -> int:
    """How many unlocated searches one graph query clears."""
    return as_int(cfg.get("gate_credit", 3), 3, minimum=0)


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


_GLOB_CHARS = set("*?[]{}")


def glob_pattern_root(pattern: str) -> Path | None:
    """The directory an absolute or `~` Glob pattern starts in, or None.

    `Glob` accepts `~/.claude/plugins/cache/**/*.py` with no `path`, and an
    unscoped search reads as a search of the whole repo — so a pattern aimed at
    the plugin cache was gated as if it searched this checkout.
    """
    raw = pattern.strip()
    if not raw:
        return None
    path = as_local_path(raw.replace("\\", "/")).expanduser()
    if not path.is_absolute():
        return None
    fixed: list[str] = []
    for part in path.parts:
        if _GLOB_CHARS & set(part):
            break
        fixed.append(part)
    return Path(*fixed) if fixed else None


def _resolve(raw: str, cwd: Path) -> Path:
    candidate = as_local_path(raw).expanduser()
    return candidate if candidate.is_absolute() else cwd / candidate


def search_tool_verdict(
    tool: str, tool_input: dict[str, object], proj: Path, cfg: dict[str, object],
    root: str = "", covers: Iterable[str] = (), exempt: Iterable[str] = (),
    graph: Path | None = None, cwd: Path | None = None,
) -> Verdict:
    """The scope rule for Grep/Glob, which name their target in `path`, or in a
    `paths` list (Copilot CLI's grep).

    No path searches the session's directory, which is the repo or a folder of it.
    """
    cwd = cwd or proj
    names = [str(tool_input.get("pattern" if tool == "Glob" else "glob") or "")]
    if only_non_source([n for n in names if n], cfg):
        return Verdict(False, "non-source-names")
    raw = tool_input.get("path")
    listed = tool_input.get("paths")
    if isinstance(raw, str) and raw.strip():
        targets = [_resolve(raw, cwd)]
    elif isinstance(listed, list) and listed:
        targets = [_resolve(p, cwd) for p in listed if isinstance(p, str) and p.strip()]
    elif tool == "Glob":
        start = glob_pattern_root(str(tool_input.get("pattern") or ""))
        targets = [start] if start is not None else []
    else:
        targets = []
    targets = targets or [cwd]
    if all(in_non_source_bundle(t) for t in targets):
        return Verdict(False, "non-source-names")
    return targets_verdict(targets, proj, root, covers, exempt, graph)


SERVER_DOWN_MESSAGE = (
    "codebase-kg: nothing answers as the codebase-kg server on port {port}, so its tools are "
    "unavailable and this search is allowed. Start it with: uv run --no-project --quiet "
    '"{cli}" server start. Then reconnect it from /mcp, or start a new session.'
)


# One run of the hook loads `shim.py` at most once: `_run` opens this dict, and the
# probe and the notice share the module. Outside a run nothing is cached.
_run_cache: dict[str, Any] | None = None


def _shim() -> Any:
    if _run_cache is None:
        return load_shim()
    if "shim" not in _run_cache:
        _run_cache["shim"] = load_shim()
    return _run_cache["shim"]


def server_answers() -> bool:
    """False only when the shared server's port is known and nothing answers as codebase-kg there."""
    try:
        shim = _shim()
        port = shim.http_port()
        return port == 0 or shim.health(port, timeout=0.5)[0] == shim.OURS
    except Exception:  # noqa: BLE001 - a failed probe keeps the gate on
        return True


def _server_down_message() -> str:
    shim = _shim()
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
    return SERVER_DOWN_MESSAGE.format(port=shim.http_port(), cli=root / "mcp" / "launch" / "kg_cli.py")


def _advise(message: str, notify_user: bool = False) -> None:
    out: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        }
    }
    if notify_user:
        out["systemMessage"] = message
    print(json.dumps(out))


def _is_search_call(
    tool: str, tool_input: dict[str, object], cfg: dict[str, object]
) -> bool:
    if tool in _SEARCH_TOOLS:
        return True
    if tool in _SHELL_TOOLS and cfg.get("gate_shell_search", True):
        command = tool_input.get("command")
        return isinstance(command, str) and is_shell_search(command)
    return False


def _tool_input(data: dict[str, object]) -> dict[str, object]:
    tool_input = data.get("tool_input") or {}
    return tool_input if isinstance(tool_input, dict) else {}


def _grant_credit(proj: Path, session: str, cfg: dict[str, object]) -> None:
    state = _read_state(proj, session)
    earned = gate_credit(cfg)
    # A grant REPLACES rather than accumulates, and takes the larger of the
    # two. Both halves earn their place: `+` would let `kg_find_by_kind` in a loop
    # bank the whole session for having learned nothing, and taking the new
    # value outright would let a cheap follow-up query cost an agent the
    # allowance a bigger answer already earned it. Running out is not
    # terminal either way — asking again tops it back up.
    state["credit"] = max(as_int(state.get("credit", 0), 0), earned)
    _write_state(proj, session, state)


def _session_cwd(data: dict[str, object], proj: Path) -> Path:
    """Where the call runs: Codex's per-call `workdir`, else the session `cwd`."""
    tool_input = _tool_input(data)
    base = data.get("cwd")
    base_dir = Path(base) if isinstance(base, str) and base.strip() else proj
    for raw in (tool_input.get("workdir"), base):
        if isinstance(raw, str) and raw.strip():
            cwd = as_local_path(raw).expanduser()
            cwd = cwd if cwd.is_absolute() else base_dir / cwd
            if cwd.is_dir():
                return cwd.resolve()
    return proj


_GATE_REASONS = {
    **REASONS,
    "gate-off": "the gate is off (search_gate or SKIP_KG)",
    "no-graph": "this repo has no code graph",
}


def evaluate(
    tool: str, tool_input: dict[str, object], proj: Path, cfg: dict[str, object],
    cwd: Path | None = None,
) -> tuple[Path | None, Verdict]:
    """The graph and the scope verdict for one call, before credit or a repeat."""
    if not _is_search_call(tool, tool_input, cfg):
        return None, Verdict(False, "not-a-search")

    if gate_mode(cfg) == "off" or skip_kg():
        return None, Verdict(False, "gate-off")

    graph = find_graph(proj, cfg)
    if graph is None:
        return None, Verdict(False, "no-graph")

    root = norm_root(str(cfg.get("root") or graph_meta(graph, "root") or ""))
    covers = parse_patterns(graph_meta(graph, "covers"))
    exempt = parse_patterns(graph_meta(graph, "exempt"))
    if tool in _SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return graph, Verdict(False, "not-a-search")
        verdict = shell_search_verdict(command, proj, cfg, root, covers, exempt, graph, cwd)
    else:
        verdict = search_tool_verdict(tool, tool_input, proj, cfg, root, covers, exempt, graph, cwd)
    return graph, verdict


def _check(
    tool: str, tool_input: dict[str, object], proj: Path, cfg: dict[str, object],
    cwd: Path | None = None,
) -> tuple[Path, Verdict] | None:
    """The graph and the verdict, or None when this call is not subject to the gate."""
    graph, verdict = evaluate(tool, tool_input, proj, cfg, cwd)
    return (graph, verdict) if graph is not None and verdict.gated else None


def _is_duplicate_handler(tool: str, data: dict[str, object]) -> bool:
    tool_use_id = data.get("tool_use_id")
    return (
        tool in _SHELL_TOOLS
        and isinstance(tool_use_id, str)
        and bool(tool_use_id)
        and not _claim_call(tool_use_id)
    )


def _search_keys(tool: str, tool_input: dict[str, object], proj: Path) -> list[str]:
    keys = [search_key(tool, tool_input)]
    subject = subject_key(tool, tool_input, proj)
    if subject:
        keys.append(subject)
    return keys


def _note_server_down(proj: Path, session: str, state: dict[str, object]) -> None:
    if not state.get("server_down_noted"):
        state["server_down_noted"] = True
        _write_state(proj, session, state)
        _advise(_server_down_message(), notify_user=True)


NOTHING_RAN = (
    "codebase-kg: nothing in this command ran. A hook can only refuse a whole {tool} "
    "call, so these parts were refused with the search: {parts}. Run them on their own, "
    "or repeat the whole command: a repeat passes the gate.\n\n"
)
_PART_LIMIT = 3
_PART_WIDTH = 80


def _nothing_ran_notice(tool: str, tool_input: dict[str, object]) -> str:
    command = tool_input.get("command")
    if tool not in _SHELL_TOOLS or not isinstance(command, str):
        return ""
    parts = other_clauses(command)
    if not parts:
        return ""
    shown = [
        "`" + (p if len(p) <= _PART_WIDTH else p[: _PART_WIDTH - 3] + "...") + "`"
        for p in parts[:_PART_LIMIT]
    ]
    if len(parts) > _PART_LIMIT:
        shown.append(f"and {len(parts) - _PART_LIMIT} more")
    return NOTHING_RAN.format(tool=tool, parts=", ".join(shown))


def _deny_reason(verdict: Verdict, state: dict[str, object], cfg: dict[str, object]) -> str:
    """A query this session leaves a `credit` key behind, so a denial after one
    names the spent credit, not a missing query."""
    if verdict.rule == "directory" and "credit" in state:
        return (
            "a directory the graph maps code under, and the "
            f"{gate_credit(cfg)} search(es) the last graph query cleared are used up"
        )
    return REASONS[verdict.rule]


def _deny(
    data: dict[str, object], proj: Path, session: str, state: dict[str, object],
    keys: list[str], cfg: dict[str, object], graph: Path, verdict: Verdict,
) -> None:
    # Record the denial before emitting, not after: a failure between the two
    # would lose the escape hatch and let the same search be refused twice.
    # A list, not one slot: two searches denied in parallel each overwrote the
    # other's record, so neither retry was recognised.
    state["denied"] = ([k for k in _denied_list(state) if k not in keys] + keys)[-_DENIED_MEMORY:]
    recorded = _write_state(proj, session, state)
    message = GATE_MESSAGE.format(
        graph=graph.name, credit=gate_credit(cfg),
        target=verdict.target or "the repo", reason=_deny_reason(verdict, state, cfg),
    )
    if gate_mode(cfg) == "warn" or not recorded:
        _advise(message, notify_user=True)
    elif data.get("agent_id"):
        # A subagent may hold no codebase-kg tool and so could never earn credit;
        # the hook cannot see its tool list, so it informs instead of denying.
        _advise(message)
    else:
        tool = str(data.get("tool_name") or "")
        emit_deny(_nothing_ran_notice(tool, _tool_input(data)) + message)


def _run(data: dict[str, object]) -> None:
    global _run_cache
    _run_cache = {}
    try:
        _gate(data)
    finally:
        _run_cache = None


def _gate(data: dict[str, object]) -> None:
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return
    tool_input = _tool_input(data)

    proj = project_dir(data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    session = str(data.get("session_id") or "-")

    cfg = load_config(proj)

    if _KG_TOOL.match(tool):
        # No graph, nothing to gate: leave no state behind.
        if find_graph(proj, cfg) is not None:
            _grant_credit(proj, session, cfg)
        return

    checked = _check(tool, tool_input, proj, cfg, _session_cwd(data, proj))
    if checked is None or _is_duplicate_handler(tool, data):
        return
    graph, verdict = checked

    state = _read_state(proj, session)
    keys = _search_keys(tool, tool_input, proj)

    # The escape hatch, checked before credit so insisting never costs any: this
    # search — or another asking for the same thing — was denied and the agent
    # is asking again. It stays open for the session: the agent has said the
    # graph cannot answer this, and making it re-win the argument on every
    # attempt is what turned the old one-shot hatch into a loop.
    if any(k in _denied_list(state) for k in keys):
        return

    credit = as_int(state.get("credit", 0), 0)
    if credit > 0:
        state["credit"] = credit - 1
        _write_state(proj, session, state)
        return

    # A denial the agent cannot clear is a trap: with the server down there is no
    # query that buys credit. Say so once per session, then stay out of the way.
    if not server_answers():
        _note_server_down(proj, session, state)
        return

    _deny(data, proj, session, state, keys, cfg, graph, verdict)


def explain(argv: list[str]) -> int:
    """`kg_cli.py gate explain`: print the scope verdict for one search, and the
    rule behind it, without reading or writing session state."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="kg_cli.py gate explain",
        description="Show whether the search gate would deny a search, and which rule decides.",
    )
    parser.add_argument("query", help="the shell command, or the Grep/Glob pattern")
    parser.add_argument("--tool", default="Bash", choices=sorted(_SHELL_TOOLS | _SEARCH_TOOLS))
    parser.add_argument("--path", help="Grep/Glob: the path argument")
    parser.add_argument("--glob", help="Grep: the glob filter")
    parser.add_argument("--cwd", help="the shell's directory (default: the current one)")
    parser.add_argument("--project", help="the project root (default: CLAUDE_PROJECT_DIR, else --cwd)")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd or os.getcwd()).resolve()
    if args.project:
        proj = Path(args.project).resolve()
    elif os.environ.get("CLAUDE_PROJECT_DIR"):
        proj = project_dir(None)
    else:
        proj = next((d for d in (cwd, *cwd.parents) if (d / ".git").exists()), cwd)
    if args.tool in _SHELL_TOOLS:
        tool_input: dict[str, object] = {"command": args.query}
    else:
        tool_input = {"pattern": args.query}
        if args.path:
            tool_input["path"] = args.path
        if args.glob:
            tool_input["glob"] = args.glob
    graph, verdict = evaluate(args.tool, tool_input, proj, load_config(proj), cwd)
    where = f"{verdict.target}: " if verdict.target else ""
    print(f"{'deny' if verdict.gated else 'allow'}  [{verdict.rule}]  {where}{_GATE_REASONS[verdict.rule]}")
    print(f"project: {proj}")
    if graph is not None:
        print(f"graph:   {graph}")
    if verdict.gated:
        print("A graph query this session, or a repeat of a denied search, lets it through.")
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "explain":
        sys.exit(explain(sys.argv[2:]))
    run_hook(_run)


if __name__ == "__main__":
    main()

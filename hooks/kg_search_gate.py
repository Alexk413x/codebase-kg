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
  a located search       a search scoped to a path the graph already anchors is
                         never gated. The agent has evidently found the file;
                         gating it would only cost a round trip.
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
import time
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (
    as_int,
    emit_deny,
    find_graph,
    graph_meta,
    is_anchored,
    load_config,
    norm_path,
    norm_root,
    parse_patterns,
    project_dir,
    reaches_mapped_code,
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
    NON_SOURCE_BUNDLES,
    NON_SOURCE_EXT,
    SearchClause,
    ShellSearch,
    _split_clauses,
    _tokens,
    clause_is_exempt,
    in_non_source_bundle,
    is_shell_search,
    only_non_source,
    parse_shell_search,
    shell_search_is_gated,
)
from _shim_loader import load_shim

__all__ = [
    "NON_SOURCE_BUNDLES",
    "NON_SOURCE_EXT",
    "_SEARCH_WORDS",
    "SearchClause",
    "ShellSearch",
    "_split_clauses",
    "_tokens",
    "clause_is_exempt",
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
    "codebase-kg: this repo has a committed code graph ({graph}). Query it before "
    "searching source.\n\n"
    "1. kg_search from the codebase-kg server (its tool name contains codebase-kg) for "
    "what you are looking for — it finds the components, not just the string. Then "
    "codebase-kg's kg_node / kg_neighborhood for anchors and relationships. The kg_search "
    "of a11y-kg or a driver searches another graph and clears nothing here.\n"
    "2. Read the anchored files to confirm current behavior. The graph is "
    "authoritative for WHERE code lives; the source is authoritative for what it "
    "does now.\n"
    "The query skill (/codebase-kg:query) is this workflow in full.\n\n"
    "A graph query clears the next {credit} search(es). A search scoped to a file "
    "the graph already anchors is never gated, and neither is one that can only "
    "match assets or project settings. And if the graph does not cover what you "
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
        for f in [*directory.glob("*.json"), *directory.glob("calls/*")]:
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


def searches_mapped_code(
    tool_input: dict[str, object], proj: Path, root: str, covers: Iterable[str] = ()
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
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = proj / candidate
    return reaches_mapped_code(candidate, proj, root, covers)


def searches_an_anchored_path(
    tool_input: dict[str, object], proj: Path, root: str, graph: Path
) -> bool:
    """Is this search pointed at a file the graph already anchors?

    An agent that names one file has already answered the question the gate
    asks — it knows where the code is. Gating that buys nothing and costs a
    round trip. Only an exact anchored file counts: a directory is where an
    agent looks when it does NOT yet know which file, which is the case the
    gate exists for.
    """
    raw = tool_input.get("path") or ""
    if not isinstance(raw, str) or not raw.strip():
        return False
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = proj / candidate
    try:
        rel = candidate.resolve().relative_to(proj).as_posix()
    except (ValueError, OSError):
        return False
    # Anchors are stored relative to the graph's `root` (SCHEMA.md §3) while a
    # search names a repo-relative path, so one side has to be translated.
    if root and rel.startswith(root + "/"):
        rel = rel[len(root) + 1:]
    return is_anchored(graph, rel)


SERVER_DOWN_MESSAGE = (
    "codebase-kg: nothing answers as the codebase-kg server on port {port}, so its tools are "
    "unavailable and this search is allowed. Start it with: uv run --no-project --quiet "
    '"{cli}" server start. Then reconnect it from /mcp, or start a new session.'
)


def server_answers() -> bool:
    """False only when the shared server's port is known and nothing answers as codebase-kg there."""
    try:
        shim = load_shim()
        port = shim.http_port()
        return port == 0 or shim.health(port, timeout=0.5)[0] == shim.OURS
    except Exception:  # noqa: BLE001 - a failed probe keeps the gate on
        return True


def _server_down_message() -> str:
    shim = load_shim()
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


def _shell_is_gated(
    tool_input: dict[str, object], proj: Path, cfg: dict[str, object], root: str,
    covers: Iterable[str] = (),
) -> bool:
    command = tool_input.get("command")
    return isinstance(command, str) and shell_search_is_gated(command, proj, cfg, root, covers)


def _search_is_gated(
    tool: str, tool_input: dict[str, object], proj: Path, cfg: dict[str, object],
    root: str, graph: Path, covers: Iterable[str] = (),
) -> bool:
    if not searches_mapped_code(tool_input, proj, root, covers):
        return False

    # A search that can only match assets or project settings is one the
    # graph has nothing to say about.
    names = [str(tool_input.get("pattern" if tool == "Glob" else "glob") or "")]
    if only_non_source([n for n in names if n], cfg):
        return False
    raw_path = tool_input.get("path")
    if isinstance(raw_path, str) and raw_path and in_non_source_bundle(Path(raw_path)):
        return False

    # The agent named a file the graph anchors — it already knows where the
    # code is, so there is nothing left to send it to the graph for.
    return not searches_an_anchored_path(tool_input, proj, root, graph)


def _graph_if_gated(
    tool: str, tool_input: dict[str, object], proj: Path, cfg: dict[str, object]
) -> Path | None:
    """The graph to consult, or None when this call is not subject to the gate."""
    if not _is_search_call(tool, tool_input, cfg):
        return None

    if gate_mode(cfg) == "off" or skip_kg():
        return None

    graph = find_graph(proj, cfg)
    if graph is None:
        return None  # no graph in this repo → nothing to consult

    root = norm_root(str(cfg.get("root") or graph_meta(graph, "root") or ""))
    covers = parse_patterns(graph_meta(graph, "covers"))
    # A shell command answers from its own text the three questions
    # `tool_input["path"]` answers for Grep/Glob: which repo, reading what, and
    # does it already name the file.
    if tool in _SHELL_TOOLS:
        gated = _shell_is_gated(tool_input, proj, cfg, root, covers)
    else:
        gated = _search_is_gated(tool, tool_input, proj, cfg, root, graph, covers)
    return graph if gated else None


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


def _deny(
    data: dict[str, object], proj: Path, session: str, state: dict[str, object],
    keys: list[str], cfg: dict[str, object], graph: Path,
) -> None:
    # Record the denial before emitting, not after: a failure between the two
    # would lose the escape hatch and let the same search be refused twice.
    # A list, not one slot: two searches denied in parallel each overwrote the
    # other's record, so neither retry was recognised.
    state["denied"] = ([k for k in _denied_list(state) if k not in keys] + keys)[-_DENIED_MEMORY:]
    recorded = _write_state(proj, session, state)
    message = GATE_MESSAGE.format(graph=graph.name, credit=gate_credit(cfg))
    if gate_mode(cfg) == "warn" or not recorded:
        _advise(message, notify_user=True)
    elif data.get("agent_id"):
        # A subagent may hold no codebase-kg tool and so could never earn credit;
        # the hook cannot see its tool list, so it informs instead of denying.
        _advise(message)
    else:
        emit_deny(message)


def _run(data: dict[str, object]) -> None:
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return
    tool_input = _tool_input(data)

    proj = project_dir(data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    session = str(data.get("session_id") or "-")

    cfg = load_config(proj)

    if _KG_TOOL.match(tool):
        _grant_credit(proj, session, cfg)
        return

    graph = _graph_if_gated(tool, tool_input, proj, cfg)
    if graph is None or _is_duplicate_handler(tool, data):
        return

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

    _deny(data, proj, session, state, keys, cfg, graph)


def main() -> None:
    run_hook(_run)


if __name__ == "__main__":
    main()

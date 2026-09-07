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
  repeat to insist       the identical search, immediately after being denied,
                         is allowed. This is the escape hatch for code the graph
                         does not cover yet, and it is what makes the gate
                         unable to strand anyone.

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
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (  # noqa: E402
    IGNORE_DIRS,
    find_graph,
    graph_meta,
    is_anchored,
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
    "The query skill (/codebase-kg:query) is this workflow in full.\n\n"
    "A graph query clears the next {credit} search(es). A search scoped to a file "
    "the graph already anchors is never gated. And if the graph does not cover "
    "what you need, run THIS SAME search again — an immediate repeat is always "
    "allowed."
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


def _read_state(proj: Path, session: str) -> dict[str, object]:
    try:
        data = json.loads(_state_path(proj, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(proj: Path, session: str, state: dict[str, object]) -> bool:
    """Persist the gate's bookkeeping. False when it could not be written.

    The caller has to know, because the escape hatch lives in this file: if the
    denial cannot be recorded, the repeat cannot be recognised, and a `deny`
    would then refuse the same search forever. The gate degrades to `warn`
    rather than take that risk.
    """
    try:
        path = _state_path(proj, session)
        path.write_text(json.dumps(state), encoding="utf-8")
        _prune(path.parent)
        return True
    except OSError:
        return False


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


def _int_setting(cfg: dict[str, object], key: str, default: int) -> int:
    """A non-negative integer setting. Negative and unparseable read as the
    default, the same posture `nudge_every` keeps: a typo costs the setting,
    never the feature."""
    try:
        value = int(cfg.get(key, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def gate_credit(cfg: dict[str, object]) -> int:
    """How many unlocated searches one graph query clears."""
    return _int_setting(cfg, "gate_credit", 3)


def credit_for(response: object, cfg: dict[str, object]) -> int:
    """What one graph answer is worth, in searches.

    Flat, because the allowance is for what the answer did NOT name: the files
    it did name are located searches, which are free.
    """
    return gate_credit(cfg)


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


# Commands whose FIRST non-flag operand is the pattern, not a path.
_PATTERN_FIRST = {
    "grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "fd", "sls",
    "select-string",
}
# `find <path> -name x` names its path first instead.
_PATH_FIRST = {"find", "get-childitem", "gci"}
_SEARCH_WORDS = _PATTERN_FIRST | _PATH_FIRST


def _split_clauses(command: str) -> list[tuple[str, bool]]:
    """Each clause of a shell command, with whether its stdin is a pipe.

    A clause fed by `|` reads the previous command's output, not the tree, so it
    is not a codebase search however much it looks like one.
    """
    parts = re.split(r"(\|\||&&|\||;|&)", command)
    out: list[tuple[str, bool]] = []
    piped = False
    for i in range(0, len(parts), 2):
        clause = parts[i].strip()
        if clause:
            out.append((clause, piped))
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        piped = sep == "|"
    return out


def _tokens(clause: str) -> list[str]:
    """Best-effort argv, with quotes stripped.

    `posix=False` because posix mode treats a backslash as an escape, which
    turns `C:\\Users\\me\\repo` into `C:Usersmerepo` — a path that resolves
    nowhere, so a search of another drive read as a search of this repo. Quotes
    survive that mode, so they come off by hand.
    """
    try:
        toks = shlex.split(clause, posix=False)
    except ValueError:
        toks = clause.split()
    out: list[str] = []
    for tok in toks:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _effective_cwd(command: str, proj: Path) -> Path:
    """Where the search actually runs, following a leading `cd`.

    The hook is told the SESSION's directory, which is not where a command that
    starts `cd elsewhere && ...` looks. Without this the gate denied searches of
    unrelated repos using this repo's graph — an answer the graph could not have
    given.
    """
    cwd = proj
    for clause, _piped in _split_clauses(command):
        toks = _tokens(clause)
        if len(toks) >= 2 and toks[0] == "cd":
            candidate = Path(toks[1])
            if not candidate.is_absolute():
                candidate = cwd / toks[1]
            try:
                cwd = candidate.resolve()
            except OSError:
                return cwd
    return cwd


def shell_search_targets(command: str, proj: Path) -> list[Path] | None:
    """The paths a shell search is aimed at, or None if it is not one.

    An empty list means "a search with no path operand": `grep foo` reads stdin
    and is not a tree search, so the caller treats it as nothing to gate.
    """
    cwd = _effective_cwd(command, proj)
    targets: list[Path] = []
    found_search = False
    for clause, piped in _split_clauses(command):
        toks = _tokens(clause)
        if not toks:
            continue
        word = toks[0].lower().lstrip("./\\")
        if word == "sudo" and len(toks) > 1:
            toks = toks[1:]
            word = toks[0].lower()
        if word not in _SEARCH_WORDS:
            continue
        if piped:
            continue  # reads the previous command's output, never the tree
        found_search = True
        operands: list[str] = []
        for tok in toks[1:]:
            if tok.startswith("-"):
                # `find <path> -name x` puts its predicates after the paths, so
                # the first flag ends the path list. Collecting past it counted
                # `-name`'s own value as a path that does not exist, and a
                # missing path reads as "still hunting" — the exact false
                # positive this function exists to remove.
                if word in _PATH_FIRST:
                    break
                continue
            operands.append(tok)
        if word in _PATTERN_FIRST and operands:
            operands = operands[1:]  # the first operand is the pattern
        for raw in operands:
            p = Path(raw)
            targets.append(p if p.is_absolute() else cwd / raw)
    if not found_search:
        return None
    return targets


def shell_search_is_gated(command: str, proj: Path) -> bool:
    """Is this shell command a search of THIS repo with no file named yet?

    One rule, and it is the same one Grep/Glob already follow: gate a search
    aimed at the mapped tree that has not already located its file. Everything
    the gate used to deny wrongly falls out of it —

      * `cd other-repo && grep -r x .`  another repo, which this graph cannot
                                        answer for;
      * `cat f | grep x`                reads a pipe, never the tree;
      * `grep x pyproject.toml`         names one file, so the question the gate
                                        asks is already answered;
      * `gh pr merge && ... && grep x f.json`
                                        no clause aimed at the tree, so the
                                        whole command stops being denied.

    A directory operand still gates: that is where you look when you do not yet
    know the file, which is the case this exists for.
    """
    targets = shell_search_targets(command, proj)
    if targets is None:
        return False  # not a search at all
    if not targets:
        return False  # no path operand: reading stdin, not the tree
    inside: list[Path] = []
    for t in targets:
        try:
            t.resolve().relative_to(proj)
        except (ValueError, OSError):
            continue  # outside this repo — not ours to gate
        inside.append(t)
    if not inside:
        return False
    # Every in-repo target already names a file → located. A directory, or a
    # path that does not exist, means the tree is still being hunted.
    return not all(t.is_file() for t in inside)


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
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = proj / raw
    try:
        rel = candidate.resolve().relative_to(proj).as_posix()
    except (ValueError, OSError):
        return False
    # Anchors are stored relative to the graph's `root` (SCHEMA.md §3) while a
    # search names a repo-relative path, so one side has to be translated.
    if root and rel.startswith(root + "/"):
        rel = rel[len(root) + 1:]
    return is_anchored(graph, rel)


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

    cfg = load_config(proj)

    # A graph query buys credit sized by what it actually handed back. On
    # PostToolUse the answer exists and can be counted; on PreToolUse it does
    # not, so that pass grants only the buffer — which keeps a query that errors
    # or that this hook cannot parse worth something rather than nothing.
    if _KG_TOOL.match(tool):
        state = _read_state(proj, session)
        earned = credit_for(data.get("tool_response"), cfg)
        # A grant REPLACES rather than accumulates, and takes the larger of the
        # two. Both halves earn their place: `+` would let `kg_stats` in a loop
        # bank the whole session for having learned nothing, and taking the new
        # value outright would let a cheap follow-up query cost an agent the
        # allowance a bigger answer already earned it. Running out is not
        # terminal either way — asking again tops it back up.
        try:
            held = int(state.get("credit", 0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            held = 0
        state["credit"] = max(held, earned)
        state.pop("denied", None)
        _write_state(proj, session, state)
        return

    if not _is_search_call(tool, tool_input, cfg):
        return

    if gate_mode(cfg) == "off" or os.environ.get("SKIP_KG"):
        return

    graph = find_graph(proj, cfg)
    if graph is None:
        return  # no graph in this repo → nothing to consult

    raw_root = str(cfg.get("root") or graph_meta(graph, "root") or "")
    root = raw_root.strip().replace("\\", "/").strip("/")
    root = "" if root == "." else root
    # A shell command answers from its own text the three questions
    # `tool_input["path"]` answers for Grep/Glob: which repo, reading what, and
    # does it already name the file.
    if tool in _SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str) or not shell_search_is_gated(command, proj):
            return
    else:
        if not searches_mapped_code(tool_input, proj, root):
            return

        # The agent named a file the graph anchors — it already knows where the
        # code is, so there is nothing left to send it to the graph for.
        if searches_an_anchored_path(tool_input, proj, root, graph):
            return

    state = _read_state(proj, session)
    key = search_key(tool, tool_input)

    # The escape hatch, checked before credit so insisting never costs any: this
    # exact search was just denied and the agent is asking again. Let it through.
    if state.get("denied") == key:
        state.pop("denied", None)
        _write_state(proj, session, state)
        return

    try:
        credit = int(state.get("credit", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        credit = 0
    if credit > 0:
        state["credit"] = credit - 1
        state.pop("denied", None)
        _write_state(proj, session, state)
        return

    # Record the denial before emitting, not after: a failure between the two
    # would lose the escape hatch and let the same search be refused twice.
    state["denied"] = key
    recorded = _write_state(proj, session, state)
    message = GATE_MESSAGE.format(graph=graph.name, credit=gate_credit(cfg))
    if gate_mode(cfg) == "warn" or not recorded:
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

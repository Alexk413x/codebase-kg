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
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (  # noqa: E402
    DEFAULTS,
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
    r"(?:^\s*|[|;&]\s*)(?:sudo\s+)?(?:grep|egrep|fgrep|rg|ripgrep|ag|ack|fd|sls|select-string)\b"
    r"|(?:^\s*|[|;&]\s*)(?:sudo\s+)?find\b[^|;&\n]*\s-(?:i?name|i?path|i?regex)\b"
    r"|(?:^\s*|[|;&]\s*)(?:get-childitem|gci)\b[^|;&\n]*\s-r(?:ecurse)?\b",
    re.IGNORECASE | re.MULTILINE,
)

# Shell redirection operators, e.g. `2>/dev/null`, `>>out.log`, `<in.txt`,
# `&>/dev/null`. None of these name a search path even though they pass the
# naive "doesn't start with a flag" check — a leftover like `2>/dev/null` read
# as a target broke the "already named a file" exemption, since that
# nonexistent path made `all(t.is_file())` false and re-triggered the gate on
# an otherwise located search.
_REDIRECT_OP = re.compile(r"^(?:[0-9]*(?:>>|>|<)|&>>?)")
# `2>&1`, `>&2`, `1>&2` — duplicates a file descriptor, names no path at all,
# so neither this token nor a following one is an operand.
_REDIRECT_DUP = re.compile(r"^[0-9]*>&[0-9]*$")

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
        d = Path(tempfile.gettempdir()) / "codebase-kg-gate" / "calls"
        d.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(tool_use_id.encode("utf-8")).hexdigest()[:16]
        os.close(os.open(d / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


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
    """Recently denied search keys. Reads the one-slot shape older versions
    wrote, so a session that spans an upgrade keeps its escape hatch."""
    raw = state.get("denied")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str)]
    return []


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
_GCI_WORDS = {"get-childitem", "gci"}
_SEARCH_WORDS = _PATTERN_FIRST | _PATH_FIRST

# Flags that take their value as the NEXT token, per tool: (short letters, long
# names). Without this, the `20` in `grep -A 20 x file` read as a path operand
# that did not exist, and a missing path reads as "still hunting" — so a grep of
# one known file was denied. Agents pass -A/-B/-C constantly, which made this the
# commonest false denial. The PowerShell tools are absent on purpose: their
# `-Pattern x -Path y` shape already parses correctly as flag-skip + operands.
_GREP_FLAGS = ("ABCmefdD", {
    "--after-context", "--before-context", "--context", "--max-count", "--regexp",
    "--file", "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "--label", "--binary-files", "--devices", "--directories",
})
_VALUE_FLAGS: dict[str, tuple[str, set[str]]] = {
    "grep": _GREP_FLAGS, "egrep": _GREP_FLAGS, "fgrep": _GREP_FLAGS,
    "rg": ("ABCmefgtTjMEdr", {
        "--after-context", "--before-context", "--context", "--max-count",
        "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
        "--threads", "--max-columns", "--encoding", "--max-depth", "--maxdepth",
        "--replace", "--sort", "--sortr", "--ignore-file", "--max-filesize",
        "--pre", "--pre-glob", "--type-add", "--engine",
    }),
    "ag": ("ABCmG", {
        "--after", "--before", "--context", "--max-count", "--ignore",
        "--file-search-regex", "--depth",
    }),
    "ack": ("ABCm", {
        "--after-context", "--before-context", "--context", "--max-count",
        "--ignore-dir", "--ignore-file",
    }),
    "fd": ("etdEj", {
        "--extension", "--type", "--max-depth", "--min-depth", "--exact-depth",
        "--exclude", "--threads", "--size", "--changed-within", "--changed-before",
        "--owner", "--max-results",
    }),
}
_VALUE_FLAGS["ripgrep"] = _VALUE_FLAGS["rg"]
# Flags whose value IS the pattern — with one of these present, the first
# operand is a path, not the pattern.
_PATTERN_FLAGS = {"-e", "--regexp", "-f", "--file"}
# Flags whose value filters by file name. When every one of them names a file
# the graph could never anchor, the search is not a question for the graph.
_NAME_FILTER_FLAGS = {"--include", "-g", "--glob", "--iglob", "-name", "-iname",
                      "-path", "-ipath"}
# `find` predicates whose value is what the search is looking for.
_FIND_TERMS = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex"}
# `Get-ChildItem` parameters: the ones that name where it looks, the ones that
# filter by file name, and the others that consume the next token.
_GCI_PATH_FLAGS = {"-path", "-literalpath", "-lp"}
_GCI_NAME_FLAGS = {"-filter", "-include"}
_GCI_VALUE_FLAGS = {"-exclude", "-depth", "-attributes"}

# Files no code graph anchors: assets, build and project settings, logs. A
# search that can only find these is one `kg_search` cannot answer, so gating it
# sent the agent to a graph with nothing to give — and it looped. Unioned with
# the repo's `exclude_ext`.
NON_SOURCE_EXT = {
    ".plist", ".xcscheme", ".xcconfig", ".pbxproj", ".entitlements",
    ".xcworkspacedata", ".xcsettings", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".pdf", ".webp", ".ico", ".heic", ".log", ".csv", ".xml", ".properties",
    ".env", ".lock", ".db", ".sqlite",
    ".xcodeproj", ".xcworkspace", ".xcassets", ".imageset", ".appiconset",
}
# Directories that hold no source, whatever their contents are called.
NON_SOURCE_BUNDLES = (
    ".xcodeproj", ".xcworkspace", ".xcassets", ".app", ".dsym", ".xcarchive",
    ".bundle", ".framework",
)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command: str) -> str:
    """The command with every heredoc body removed.

    A heredoc body is data, not commands — a commit message that discusses
    `grep` is not a search. An unterminated one swallows the rest, which errs
    toward not gating.
    """
    lines = command.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        kept.append(lines[i])
        match = _HEREDOC.search(lines[i])
        i += 1
        if match:
            delim = match.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # the closing delimiter is not a command either
    return "\n".join(kept)


def _split_clauses(command: str) -> list[tuple[str, bool]]:
    """Each clause of a shell command, with whether its stdin is a pipe.

    A clause fed by `|` reads the previous command's output, not the tree, so it
    is not a codebase search however much it looks like one.

    Quote-aware: a `|`, `;`, `&`, or newline inside a quoted string is data, not
    a separator — `grep "a\\|b"` is one clause, not two. A regex alternation
    pattern is an ordinary way to call grep, and splitting on the separator
    hiding inside it manufactured a bogus trailing "clause" (a fragment of the
    pattern read as a path), which is exactly the false positive this function
    exists to avoid.
    """
    parts: list[str] = []
    seps: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        two = command[i:i + 2]
        # A newline separates commands as surely as `;` does — without it a
        # search on its own line stays glued to whatever ran above it and is
        # never seen.
        if two in ("&&", "||"):
            parts.append("".join(buf))
            seps.append(two)
            buf = []
            i += 2
            continue
        if ch in "|;&\n":
            parts.append("".join(buf))
            seps.append(ch)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))

    out: list[tuple[str, bool]] = []
    piped = False
    for idx, part in enumerate(parts):
        clause = part.strip()
        if clause:
            out.append((clause, piped))
        piped = idx < len(seps) and seps[idx] == "|"
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
            candidate = Path(toks[1]).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                cwd = candidate.resolve()
            except OSError:
                return cwd
    return cwd


class SearchClause:
    """One search command within a shell line: where it looks, and among which
    file names."""

    def __init__(self, word: str) -> None:
        self.word = word
        self.targets: list[Path] = []
        # File-name filters (`--include`, `-g`, `find -name`). Empty means the
        # clause can reach any file.
        self.names: list[str] = []
        # True when the filters are alternatives (`find -o`), so every one must
        # be non-source; otherwise they narrow each other and one is enough.
        self.names_any = False


class ShellSearch:
    """What one shell command searches: where, for what, and among which names."""

    def __init__(self) -> None:
        self.clauses: list[SearchClause] = []
        # What is being looked for — grep patterns, `find -name` values. Two
        # searches for the same terms are the same question, however phrased.
        self.terms: list[str] = []

    @property
    def targets(self) -> list[Path]:
        return [t for c in self.clauses for t in c.targets]


def _value_flag(word: str, tok: str) -> tuple[str, str | None] | None:
    """If `tok` is a flag of `word` that takes a value: (its canonical name, the
    attached value — or None when the value is the NEXT token). None for any
    other token.

    Short clusters count: in `-nA 3` the `A` takes `3`; in `-A3` the value is
    attached, so nothing further is consumed.
    """
    shorts, longs = _VALUE_FLAGS.get(word, ("", set()))
    if tok.startswith("--"):
        name, eq, value = tok.partition("=")
        if name not in longs:
            return None
        return name, (value if eq else None)
    if len(tok) < 2:
        return None
    for i, ch in enumerate(tok[1:], start=1):
        if ch in shorts:
            return f"-{ch}", (tok[i + 1:] or None)
        if not ch.isalpha():
            return None  # e.g. `-5`, or a digit run that is itself a value
    return None


def _unquote(value: str) -> str:
    """`--include="*.py"` survives tokenizing with its quotes on the value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _is_unexpanded(raw: str) -> bool:
    """A variable or command substitution, which only a real shell can resolve.

    Read literally, `$S/test.log` is a path that does not exist, and a missing
    path reads as "still hunting". What it names is unknowable here, so it is no
    evidence either way.
    """
    return "$" in raw or "`" in raw


def parse_shell_search(command: str, proj: Path) -> ShellSearch | None:
    """What a shell command searches, or None if it is not a search at all."""
    command = strip_heredocs(command)
    cwd = _effective_cwd(command, proj)
    out = ShellSearch()
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
        clause_out = SearchClause(word)
        out.clauses.append(clause_out)
        operands: list[str] = []
        names = clause_out.names
        pattern_flagged = False
        in_predicates = False  # `find`: past the path list, into its predicates
        options_done = False   # after `--`, everything is an operand
        pending: str | None = None  # a flag whose value is the next token
        skip_next = False
        positional = 0
        for tok in toks[1:]:
            if skip_next:
                skip_next = False
                continue
            if pending is not None and word in _GCI_WORDS:
                if pending in _GCI_PATH_FLAGS:
                    operands.append(tok)
                elif pending in _GCI_NAME_FLAGS:
                    names.append(_unquote(tok))
                    out.terms.append(tok)
                pending = None
                continue
            if pending is not None:
                if pending in _NAME_FILTER_FLAGS:
                    names.append(_unquote(tok))
                if pending in _PATTERN_FLAGS or pending in _FIND_TERMS:
                    out.terms.append(tok)
                pending = None
                continue
            if _REDIRECT_DUP.match(tok):
                continue  # e.g. "2>&1" — duplicates a descriptor, names no path
            redirect = _REDIRECT_OP.match(tok)
            if redirect:
                if redirect.end() == len(tok):
                    skip_next = True  # bare operator; the NEXT token is its target
                continue  # a redirection operand is never a search path
            if word in _GCI_WORDS:
                low = tok.lower()
                if low.startswith("-"):
                    if low in _GCI_PATH_FLAGS | _GCI_NAME_FLAGS | _GCI_VALUE_FLAGS:
                        pending = low
                    continue
                positional += 1
                if positional == 2:
                    names.append(_unquote(tok))
                    out.terms.append(tok)
                else:
                    operands.append(tok)
                continue
            if word in _PATH_FIRST:
                # `find <path> -name x` puts its predicates after the paths, so
                # the first flag ends the path list. Collecting past it counted
                # `-name`'s own value as a path that does not exist, and a
                # missing path reads as "still hunting" — the exact false
                # positive this function exists to remove.
                if in_predicates or tok.startswith("-") or tok in {"(", "!", "\\("}:
                    in_predicates = True
                    if tok.lower() in {"-o", "-or"}:
                        clause_out.names_any = True
                    if tok.lower() in _FIND_TERMS:
                        pending = tok.lower()
                    continue
                operands.append(tok)
                continue
            if tok == "--" and not options_done:
                options_done = True
                continue
            if tok.startswith("-") and not options_done:
                flag = _value_flag(word, tok)
                if flag is not None:
                    name, value = flag
                    if name in _PATTERN_FLAGS:
                        pattern_flagged = True
                    if value is None:
                        pending = name
                    else:
                        value = _unquote(value)
                        if name in _NAME_FILTER_FLAGS:
                            names.append(value)
                        if name in _PATTERN_FLAGS:
                            out.terms.append(value)
                elif word in {"rg", "ripgrep"} and tok == "--files":
                    pattern_flagged = True  # lists files: no pattern operand
                continue
            operands.append(tok)
        if word in _PATTERN_FIRST and operands and not pattern_flagged:
            out.terms.append(operands[0])
            operands = operands[1:]  # the first operand is the pattern
        if word in _GCI_WORDS and not operands:
            operands = ["."]
        for raw in operands:
            if _is_unexpanded(raw):
                continue
            # `~`/`~user` only means home when a real shell expands it — but a
            # search naming one is always aimed outside a project checkout, and
            # reading it literally instead joined it under `cwd`, turning an
            # out-of-repo search into a bogus path this project's gate then
            # claimed as its own.
            p = Path(raw).expanduser()
            clause_out.targets.append(p if p.is_absolute() else cwd / p)
    return out if found_search else None


def shell_search_targets(command: str, proj: Path) -> list[Path] | None:
    """The paths a shell search is aimed at, or None if it is not one.

    An empty list means "a search with no path operand": `grep foo` reads stdin
    and is not a tree search, so the caller treats it as nothing to gate.
    """
    parsed = parse_shell_search(command, proj)
    return None if parsed is None else parsed.targets


def _name_ext(name: str) -> str:
    """The extension a file-name filter pins, or "" when it pins none.

    `*.plist` and `GoogleService-Info.plist` pin `.plist`; `*Splash*` and
    `*.sw?` pin nothing — the latter could still be source.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    ext = "." + base.rsplit(".", 1)[1].lower()
    return "" if any(c in ext for c in "*?[]{}") else ext


def _expand_braces(name: str) -> list[str]:
    """`*.{json,plist}` → `*.json`, `*.plist`. One level, which is all a file
    filter ever uses in practice."""
    m = re.search(r"\{([^{}]*)\}", name)
    if not m:
        return [name]
    return [name[:m.start()] + alt + name[m.end():] for alt in m.group(1).split(",")]


def only_non_source(
    names: list[str], cfg: dict[str, object], alternatives: bool = True
) -> bool:
    """Do these file-name filters only ever match files no graph anchors?

    `alternatives` says how the filters combine. `--include a --include b` and
    `find -name a -o -name b` accept a file matching ANY of them, so every one
    must be non-source. `find -name '*.xcscheme' -path '*/shared/*'` requires
    ALL of them, so one non-source filter already rules out source.

    False for an empty list: a search with no name filter can reach any file.
    """
    if not names:
        return False
    excluded = NON_SOURCE_EXT | {
        str(e).lower() for e in cfg.get("exclude_ext", []) or []  # type: ignore[union-attr]
    }

    def non_source(name: str) -> bool:
        return all(_name_ext(alt) in excluded for alt in _expand_braces(name))

    verdicts = [non_source(n) for n in names]
    return all(verdicts) if alternatives else any(verdicts)


def in_non_source_bundle(path: Path) -> bool:
    """Is this path inside an Xcode project, asset catalog, or built bundle?

    Nothing in one is code a graph anchors, so a search scoped there is not a
    question for the graph however it is phrased.
    """
    return any(part.lower().endswith(NON_SOURCE_BUNDLES) for part in path.parts)


def clause_is_exempt(clause: SearchClause, cfg: dict[str, object]) -> bool:
    """Can this search clause only ever find files no graph anchors?"""
    # grep/rg name filters are always alternatives; only `find` ANDs its own.
    alternatives = clause.names_any or clause.word not in _PATH_FIRST
    if only_non_source(clause.names, cfg, alternatives=alternatives):
        return True
    return bool(clause.targets) and all(in_non_source_bundle(t) for t in clause.targets)



def shell_search_is_gated(
    command: str, proj: Path, cfg: dict[str, object] | None = None
) -> bool:
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

      * `find . -name Info.plist`       can only find files no graph anchors;
      * `grep -A 5 x f.py`, `grep x $F` a flag's value or a shell variable is
                                        not a missing path.

    A directory operand still gates: that is where you look when you do not yet
    know the file, which is the case this exists for.
    """
    parsed = parse_shell_search(command, proj)
    if parsed is None:
        return False  # not a search at all
    # A clause that can only find files the graph never holds contributes no
    # targets: it is not a question the graph can answer.
    targets = [
        t for c in parsed.clauses if not clause_is_exempt(c, cfg or DEFAULTS) for t in c.targets
    ]
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
    return bool(_SHELL_SEARCH.search(strip_heredocs(command)))


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
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = proj / candidate
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


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


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

    if _KG_TOOL.match(tool):
        state = _read_state(proj, session)
        earned = gate_credit(cfg)
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
        if not isinstance(command, str) or not shell_search_is_gated(command, proj, cfg):
            return
    else:
        if not searches_mapped_code(tool_input, proj, root):
            return

        # A search that can only match assets or project settings is one the
        # graph has nothing to say about.
        names = [str(tool_input.get("pattern" if tool == "Glob" else "glob") or "")]
        if only_non_source([n for n in names if n], cfg):
            return
        raw_path = tool_input.get("path")
        if isinstance(raw_path, str) and raw_path and in_non_source_bundle(Path(raw_path)):
            return

        # The agent named a file the graph anchors — it already knows where the
        # code is, so there is nothing left to send it to the graph for.
        if searches_an_anchored_path(tool_input, proj, root, graph):
            return

    tool_use_id = data.get("tool_use_id")
    if tool in _SHELL_TOOLS and isinstance(tool_use_id, str) and tool_use_id:
        if not _claim_call(tool_use_id):
            return

    state = _read_state(proj, session)
    keys = [search_key(tool, tool_input)]
    subject = subject_key(tool, tool_input, proj)
    if subject:
        keys.append(subject)
    denied = _denied_list(state)

    # The escape hatch, checked before credit so insisting never costs any: this
    # search — or another asking for the same thing — was denied and the agent
    # is asking again. It stays open for the session: the agent has said the
    # graph cannot answer this, and making it re-win the argument on every
    # attempt is what turned the old one-shot hatch into a loop.
    if any(k in denied for k in keys):
        return

    try:
        credit = int(state.get("credit", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        credit = 0
    if credit > 0:
        state["credit"] = credit - 1
        _write_state(proj, session, state)
        return

    # Record the denial before emitting, not after: a failure between the two
    # would lose the escape hatch and let the same search be refused twice.
    # A list, not one slot: two searches denied in parallel each overwrote the
    # other's record, so neither retry was recognised.
    state["denied"] = ([k for k in denied if k not in keys] + keys)[-_DENIED_MEMORY:]
    recorded = _write_state(proj, session, state)
    message = GATE_MESSAGE.format(graph=graph.name, credit=gate_credit(cfg))
    if gate_mode(cfg) == "warn" or not recorded:
        _advise(message, notify_user=True)
    elif data.get("agent_id"):
        # A subagent may hold no codebase-kg tool and so could never earn credit;
        # the hook cannot see its tool list, so it informs instead of denying.
        _advise(message)
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

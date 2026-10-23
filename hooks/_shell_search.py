"""Shell-command parsing for the search gate. Stdlib only.

Reads a Bash or PowerShell command line and answers what it searches: which
clauses are search commands, where each looks, for what, and among which file
names. The gate decision itself lives in `kg_search_gate.py`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path

from _config import DEFAULTS, as_local_path, reaches_mapped_code, strip_heredocs

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


def _separator_at(command: str, i: int) -> str:
    two = command[i:i + 2]
    if two in ("&&", "||"):
        return two
    ch = command[i]
    return ch if ch in "|;&\n" else ""


def _split_raw(command: str) -> tuple[list[str], list[str]]:
    parts: list[str] = []
    seps: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            step = 2 if ch == "\\" and quote == '"' and i + 1 < n else 1
            buf.append(command[i:i + step])
            if ch == quote:
                quote = None
            i += step
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(command[i:i + 2])
            i += 2
            continue
        # A newline separates commands as surely as `;` does — without it a
        # search on its own line stays glued to whatever ran above it and is
        # never seen.
        sep = _separator_at(command, i)
        if sep:
            parts.append("".join(buf))
            seps.append(sep)
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts, seps


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
    parts, seps = _split_raw(command)
    out: list[tuple[str, bool]] = []
    piped = False
    for idx, part in enumerate(parts):
        clause = part.strip()
        if clause:
            out.append((clause, piped))
        piped = idx < len(seps) and seps[idx] == "|"
    return out


class _Quoted(str):
    """A token that was quoted, so `"<Button"` is a pattern and not a redirect."""


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
            tok = _Quoted(tok[1:-1])
        out.append(tok)
    return out


def _after_cd(toks: list[str], cwd: Path) -> Path:
    """Where the next clause runs: a `cd` moves later clauses, never earlier ones.

    The hook is told the SESSION's directory, which is not where a command that
    starts `cd elsewhere && ...` looks. Without this the gate denied searches of
    unrelated repos using this repo's graph — an answer the graph could not have
    given.
    """
    if len(toks) < 2 or toks[0] != "cd":
        return cwd
    candidate = as_local_path(toks[1]).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
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


class _ClauseScan:
    """Reads the arguments of one search command, a token at a time."""

    def __init__(self, word: str, clause: SearchClause, terms: list[str]) -> None:
        self.word = word
        self.names = clause.names
        self.clause = clause
        self.terms = terms
        self.operands: list[str] = []
        self.pattern_flagged = False
        self.in_predicates = False  # `find`: past the path list, into its predicates
        self.options_done = False   # after `--`, everything is an operand
        self.pending: str | None = None  # a flag whose value is the next token
        self.skip_next = False
        self.positional = 0

    def feed(self, tok: str) -> None:
        if self.skip_next:
            self.skip_next = False
        elif self.pending is not None:
            self._take_value(tok)
        elif self._is_redirect(tok):
            return
        elif self.word in _GCI_WORDS:
            self._powershell_arg(tok)
        elif self.word in _PATH_FIRST:
            self._find_arg(tok)
        else:
            self._grep_arg(tok)

    def _take_value(self, tok: str) -> None:
        pending = self.pending
        self.pending = None
        if self.word in _GCI_WORDS:
            if pending in _GCI_PATH_FLAGS:
                self.operands.append(tok)
            elif pending in _GCI_NAME_FLAGS:
                self.names.append(_unquote(tok))
                self.terms.append(tok)
            return
        if pending in _NAME_FILTER_FLAGS:
            self.names.append(_unquote(tok))
        if pending in _PATTERN_FLAGS or pending in _FIND_TERMS:
            self.terms.append(tok)

    def _is_redirect(self, tok: str) -> bool:
        redirect = None if isinstance(tok, _Quoted) else _REDIRECT_OP.match(tok)
        if not redirect:
            return False
        if redirect.end() == len(tok):
            self.skip_next = True  # bare operator; the NEXT token is its target
        return True  # a redirection operand is never a search path

    def _powershell_arg(self, tok: str) -> None:
        low = tok.lower()
        if low.startswith("-"):
            if low in _GCI_PATH_FLAGS | _GCI_NAME_FLAGS | _GCI_VALUE_FLAGS:
                self.pending = low
            return
        self.positional += 1
        if self.positional == 2:
            self.names.append(_unquote(tok))
            self.terms.append(tok)
        else:
            self.operands.append(tok)

    def _find_arg(self, tok: str) -> None:
        # `find <path> -name x` puts its predicates after the paths, so
        # the first flag ends the path list. Collecting past it counted
        # `-name`'s own value as a path that does not exist, and a
        # missing path reads as "still hunting" — the exact false
        # positive this function exists to remove.
        if self.in_predicates or tok.startswith("-") or tok in {"(", "!", "\\("}:
            self.in_predicates = True
            if tok.lower() in {"-o", "-or"}:
                self.clause.names_any = True
            if tok.lower() in _FIND_TERMS:
                self.pending = tok.lower()
            return
        self.operands.append(tok)

    def _grep_arg(self, tok: str) -> None:
        if tok == "--" and not self.options_done:
            self.options_done = True
        elif tok.startswith("-") and not self.options_done:
            self._grep_flag(tok)
        else:
            self.operands.append(tok)

    def _grep_flag(self, tok: str) -> None:
        flag = _value_flag(self.word, tok)
        if flag is None:
            if self.word in {"rg", "ripgrep"} and tok == "--files":
                self.pattern_flagged = True  # lists files: no pattern operand
            return
        name, value = flag
        if name in _PATTERN_FLAGS:
            self.pattern_flagged = True
        if value is None:
            self.pending = name
            return
        value = _unquote(value)
        if name in _NAME_FILTER_FLAGS:
            self.names.append(value)
        if name in _PATTERN_FLAGS:
            self.terms.append(value)

    def targets(self, cwd: Path) -> list[Path]:
        operands = self.operands
        if self.word in _PATTERN_FIRST and operands and not self.pattern_flagged:
            self.terms.append(operands[0])
            operands = operands[1:]  # the first operand is the pattern
        if self.word in _GCI_WORDS and not operands:
            operands = ["."]
        targets: list[Path] = []
        for raw in operands:
            if _is_unexpanded(raw):
                continue
            # `~`/`~user` only means home when a real shell expands it — but a
            # search naming one is always aimed outside a project checkout, and
            # reading it literally instead joined it under `cwd`, turning an
            # out-of-repo search into a bogus path this project's gate then
            # claimed as its own.
            p = as_local_path(raw).expanduser()
            targets.append(p if p.is_absolute() else cwd / p)
        return targets


def _search_word(toks: list[str]) -> tuple[str, list[str]]:
    word = toks[0].lower().lstrip("./\\")
    if word == "sudo" and len(toks) > 1:
        toks = toks[1:]
        word = toks[0].lower()
    return word, toks


def parse_shell_search(command: str, proj: Path) -> ShellSearch | None:
    """What a shell command searches, or None if it is not a search at all."""
    command = strip_heredocs(command)
    cwd = proj
    out = ShellSearch()
    for clause, piped in _split_clauses(command):
        toks = _tokens(clause)
        if not toks:
            continue
        cwd = _after_cd(toks, cwd)
        word, toks = _search_word(toks)
        if word not in _SEARCH_WORDS:
            continue
        if piped:
            continue  # reads the previous command's output, never the tree
        clause_out = SearchClause(word)
        out.clauses.append(clause_out)
        scan = _ClauseScan(word, clause_out, out.terms)
        for tok in toks[1:]:
            scan.feed(tok)
        clause_out.targets.extend(scan.targets(cwd))
    return out if out.clauses else None


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
    command: str, proj: Path, cfg: dict[str, object] | None = None, root: str = "",
    covers: Iterable[str] = (),
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
    # Outside this repo, outside the graph's `root`, or in an ignored dir: not
    # ours to gate.
    inside = [t for t in targets if reaches_mapped_code(t, proj, root, covers)]
    if not inside:
        return False
    # Every in-repo target already names a file → located. A directory, or a
    # path that does not exist, means the tree is still being hunted.
    return not all(t.is_file() for t in inside)


def is_shell_search(command: str) -> bool:
    return bool(_SHELL_SEARCH.search(strip_heredocs(command)))

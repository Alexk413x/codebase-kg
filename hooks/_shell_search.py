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
from typing import NamedTuple

from _config import (
    DEFAULTS,
    anchors_under,
    as_local_path,
    reaches_mapped_code,
    root_relative,
    strip_heredocs,
)

# Shell commands that are a codebase search by another name. Deliberately narrow:
# a false positive here denies an unrelated command, which is worse than missing
# one. `find` and `Get-ChildItem` only count when they carry a name/path filter,
# otherwise an ordinary `find . -type d` trips the gate.
# What may stand in front of the command word and leaves it the command: `sudo`, `env`, `command`,
# and `NAME=value` assignments. Without these, `X=1 grep -rn x src` was not read as a search at all.
# The three value branches start with different characters. If the bare branch matched a quote too,
# a run of quotes would backtrack exponentially, and a hook that times out allows the command.
_LEAD = (
    r"(?:(?:sudo|env|command)\s+"
    r"|[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s|;&\"'])*\s+)*"
)
_SHELL_SEARCH = re.compile(
    r"(?:^\s*|[|;&]\s*)" + _LEAD + r"(?:grep|egrep|fgrep|rg|ripgrep|ag|ack|fd|sls|select-string)\b"
    r"|(?:^\s*|[|;&]\s*)" + _LEAD + r"find\b[^|;&\n]*\s-(?:i?name|i?path|i?regex)\b"
    r"|(?:^\s*|[|;&]\s*)(?:get-childitem|gci)\b[^|;&\n]*\s-r(?:ecurse)?\b"
    r"|(?:^\s*|[|;&]\s*)" + _LEAD + r"git\b(?:\s+-[Cc]\s+\S+|\s+-\S+)*\s+grep\b"
    r"|\|\s*xargs\b[^|;&\n]*\s(?:grep|egrep|fgrep|rg|ripgrep|ag|ack)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Shell redirection operators, e.g. `2>/dev/null`, `>>out.log`, `<in.txt`,
# `&>/dev/null`. None of these names a search path, though each passes the
# "doesn't start with a flag" check: read as a target, `>out.log` would take part
# in the verdict.
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
# names). Without this, the `20` in `grep -A 20 x src` reads as the pattern and
# `x` as a path, so both the search's terms and its targets come out wrong.
# Agents pass -A/-B/-C constantly. The PowerShell tools are absent on purpose: their
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
# `git grep`, which is not one of the shell words: it starts behind `git`.
_GIT_GREP = "git-grep"
_VALUE_FLAGS[_GIT_GREP] = ("ABCmefO", {
    "--after-context", "--before-context", "--context", "--max-count", "--regexp",
    "--file", "--max-depth", "--threads", "--open-files-in-pager",
})
# `git` options before the subcommand that take the next token as their value.
_GIT_VALUE_OPTS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# `xargs` options that take the next token as their value.
_XARGS_VALUE_OPTS = {"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a"}
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
# `Select-String` names its parameters, so `-Path x -Pattern y` must not read
# `x` as the pattern and `y` as a path inside the repo.
_SLS_WORDS = {"sls", "select-string"}
_SLS_PATH_FLAGS = {"-path", "-literalpath", "-lp", "-pspath"}
_SLS_PATTERN_FLAGS = {"-pattern"}
_SLS_NAME_FLAGS = {"-include"}
_SLS_VALUE_FLAGS = {"-exclude", "-encoding", "-context", "-culture", "-inputobject"}
# Commands that move the working directory of the clauses after them.
_CD_WORDS = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}

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


def _cd_target(toks: list[str]) -> str | None:
    args = [t for t in toks[1:] if not (t.startswith("-") and not isinstance(t, _Quoted))]
    return args[0] if args else None


def _after_cd(toks: list[str], cwd: Path | None) -> Path | None:
    """Where the next clause runs: a `cd` moves later clauses, never earlier ones.

    The hook is told the SESSION's directory, which is not where a command that
    starts `cd elsewhere && ...` looks. Without this the gate denied searches of
    unrelated repos using this repo's graph — an answer the graph could not have
    given. None means a `cd` to a place only a real shell can resolve, such as
    `cd "$HOME/x"`: relative paths after it name nothing this hook can place.
    """
    if not toks or toks[0].lower() not in _CD_WORDS:
        return cwd
    raw = _cd_target(toks)
    if raw is None:
        return None  # a bare `cd` goes home, which is never this checkout
    if _is_unexpanded(raw):
        return None
    candidate = as_local_path(raw).expanduser()
    if not candidate.is_absolute():
        if cwd is None:
            return None
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
        # True when an operand names a place only the shell can resolve.
        self.unplaced = False
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

    Read literally, `$S/test.log` is a path that does not exist. What it names
    is unknowable here, so it is no evidence either way.
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
        self.recursive = False
        self.stdin_redirected = False
        self.dashdash_at: int | None = None  # operands from here on follow `--`

    def feed(self, tok: str) -> None:
        if self.skip_next:
            self.skip_next = False
        elif self.pending is not None:
            self._take_value(tok)
        elif self._is_redirect(tok):
            return
        elif self.word in _GCI_WORDS:
            self._powershell_arg(tok)
        elif self.word in _SLS_WORDS:
            self._sls_arg(tok)
        elif self.word in _PATH_FIRST:
            self._find_arg(tok)
        else:
            self._grep_arg(tok)

    def _take_value(self, tok: str) -> None:
        pending = self.pending
        self.pending = None
        if self.word in _SLS_WORDS:
            if pending in _SLS_PATH_FLAGS:
                self.operands.append(tok)
            elif pending in _SLS_PATTERN_FLAGS:
                self.terms.append(tok)
            elif pending in _SLS_NAME_FLAGS:
                self.names.append(_unquote(tok))
            return
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
        if tok.lstrip("0123456789").startswith("<"):
            self.stdin_redirected = True
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

    def _sls_arg(self, tok: str) -> None:
        low = tok.lower()
        if low.startswith("-") and not isinstance(tok, _Quoted):
            if low in _SLS_PATTERN_FLAGS:
                self.pattern_flagged = True
            if low in _SLS_PATH_FLAGS | _SLS_PATTERN_FLAGS | _SLS_NAME_FLAGS | _SLS_VALUE_FLAGS:
                self.pending = low
            return
        self.operands.append(tok)

    def _find_arg(self, tok: str) -> None:
        # `find <path> -name x` puts its predicates after the paths, so
        # the first flag ends the path list. Collecting past it reads
        # `-name`'s own value as a path.
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
            self.dashdash_at = len(self.operands)
        elif tok.startswith("-") and not self.options_done:
            self._grep_flag(tok)
        else:
            self.operands.append(tok)

    def _grep_flag(self, tok: str) -> None:
        if tok in {"--recursive", "--dereference-recursive"} or (
            not tok.startswith("--") and re.match(r"^-[A-Za-z]*[rR]", tok)
            and self.word in {"grep", "egrep", "fgrep"}
        ):
            self.recursive = True
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

    def targets(self, cwd: Path | None) -> list[Path]:
        operands = self.operands
        dashdash = len(operands) if self.dashdash_at is None else self.dashdash_at
        pattern_first = self.word in _PATTERN_FIRST or self.word == _GIT_GREP
        if pattern_first and operands and not self.pattern_flagged:
            self.terms.append(operands[0])
            operands = operands[1:]  # the first operand is the pattern
            dashdash = max(dashdash - 1, 0)
        if self.word == _GIT_GREP:
            # Before `--`, an operand may be a revision rather than a path; only
            # one that exists is read as a path.
            operands = [
                o for i, o in enumerate(operands)
                if i >= dashdash or _is_unexpanded(o) or _exists(o, cwd)
            ]
            # Pathspecs that are all globs, `-- '*.md'`, filter by file name.
            if operands and all(any(c in o for c in "*?[") for o in operands):
                self.names.extend(_unquote(o) for o in operands)
        if not operands and self._searches_cwd_by_default():
            operands = ["."]
        targets: list[Path] = []
        for raw in operands:
            if _is_unexpanded(raw):
                self.clause.unplaced = True
                continue
            raw = _glob_free_prefix(raw)
            # `~`/`~user` only means home when a real shell expands it — but a
            # search naming one is always aimed outside a project checkout, and
            # reading it literally instead joined it under `cwd`, turning an
            # out-of-repo search into a bogus path this project's gate then
            # claimed as its own.
            p = as_local_path(raw).expanduser()
            if p.is_absolute():
                targets.append(p)
            elif cwd is not None:
                targets.append(cwd / p)
            else:
                self.clause.unplaced = True
        return targets

    def _searches_cwd_by_default(self) -> bool:
        """With no path operand, these walk the working directory rather than
        read stdin. `grep` does so only when recursive."""
        if self.stdin_redirected:
            return False
        if self.word in {"grep", "egrep", "fgrep"}:
            return self.recursive
        return self.word not in {"sls", "select-string"}


def _glob_free_prefix(raw: str) -> str:
    """`src/*.ts` → `src`: the shell expands a glob after the hook has run, so the
    search is aimed at the directory the glob sits in."""
    if not any(c in raw for c in "*?["):
        return raw
    parts = raw.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if any(c in part for c in "*?["):
            return "/".join(parts[:i]) or "."
    return raw


def _exists(raw: str, cwd: Path | None) -> bool:
    p = as_local_path(raw).expanduser()
    if not p.is_absolute():
        if cwd is None:
            return False
        p = cwd / p
    return p.exists()


def _git_subcommand(
    toks: list[str], cwd: Path | None
) -> tuple[str, list[str], Path | None]:
    """`git -C dir grep x` → ("grep", [the tokens from `grep` on], dir)."""
    i = 1
    while i < len(toks):
        tok = toks[i]
        if tok == "-C" and i + 1 < len(toks):
            target = as_local_path(toks[i + 1]).expanduser()
            if target.is_absolute():
                cwd = target
            elif cwd is not None:
                cwd = cwd / target
            i += 2
        elif tok in _GIT_VALUE_OPTS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tok.lower(), toks[i:], cwd
    return "", [], cwd


def _xargs_command(toks: list[str]) -> list[str]:
    """The command `xargs` runs, from its first word on."""
    i = 1
    while i < len(toks):
        tok = toks[i]
        if tok in _XARGS_VALUE_OPTS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return toks[i:]
    return []


def _listed_paths(toks: list[str], cwd: Path | None) -> list[Path]:
    """The paths a `git ls-files` lists: its pathspecs, or the whole tree."""
    specs = [t for t in toks[1:] if not t.startswith("-")] or ["."]
    out = []
    for raw in specs:
        p = as_local_path(_glob_free_prefix(raw)).expanduser()
        if p.is_absolute():
            out.append(p)
        elif cwd is not None:
            out.append(cwd / p)
    return out


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_LEAD_WORDS = {"sudo", "env", "command"}


def _search_word(toks: list[str]) -> tuple[str, list[str]]:
    """The command word of a clause and the tokens from it on, past `sudo`, `env`, `command` and
    `NAME=value` assignments. Those change the environment a command runs in, not which command runs."""
    start = 0
    while start < len(toks) - 1:
        tok = toks[start]
        if tok.lower() in _LEAD_WORDS:
            start += 1
        elif not isinstance(tok, _Quoted) and _ASSIGNMENT.match(tok):
            start = _past_value(toks, start)
        else:
            break
    toks = toks[min(start, len(toks) - 1):]
    return toks[0].lower().lstrip("./\\"), toks


def _past_value(toks: list[str], at: int) -> int:
    """The index after the assignment at `at`. `_tokens` splits `NAME="two words"` at the space, so a
    value that opens a quote runs to the token that closes it."""
    for quote in "\"'":
        if toks[at].count(quote) % 2:
            for end in range(at + 1, len(toks)):
                if quote in toks[end]:
                    return end + 1
            return len(toks)
    return at + 1


def parse_shell_search(
    command: str, proj: Path, cwd: Path | None = None
) -> ShellSearch | None:
    """What a shell command searches, or None if it is not a search at all.

    `cwd` is where the shell starts, which is the session's directory and not
    necessarily the project root: the Bash tool keeps a `cd` from an earlier call.
    """
    command = strip_heredocs(command)
    cwd = cwd or proj
    out = ShellSearch()
    listed: list[Path] | None = None  # what the previous clause, a `git ls-files`, lists
    for clause, piped in _split_clauses(command):
        files_from, listed = (listed if piped else None), None
        # The parentheses of a subshell, `(cd x && grep y .)`, are not part of
        # its first word or its last operand.
        toks = _tokens(clause.lstrip("( ").rstrip(") "))
        if not toks:
            continue
        cwd = _after_cd(toks, cwd)
        word, toks = _search_word(toks)
        here = cwd
        if word == "git":
            sub, toks, here = _git_subcommand(toks, cwd)
            if sub == "ls-files":
                listed = _listed_paths(toks, here)
                continue
            if sub != "grep":
                continue
            word = _GIT_GREP
        elif word == "xargs":
            # `git ls-files | xargs grep x` searches every file git lists. Any
            # other list on stdin names its files, which is a located search.
            inner = _xargs_command(toks)
            if files_from is None or not inner or _search_word(inner)[0] not in _SEARCH_WORDS:
                continue
            clause_out = SearchClause(_search_word(inner)[0])
            clause_out.targets.extend(files_from)
            out.clauses.append(clause_out)
            continue
        elif word not in _SEARCH_WORDS:
            continue
        if piped:
            continue  # reads the previous command's output, never the tree
        clause_out = SearchClause(word)
        out.clauses.append(clause_out)
        scan = _ClauseScan(word, clause_out, out.terms)
        for tok in toks[1:]:
            scan.feed(tok)
        clause_out.targets.extend(scan.targets(here))
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


class Verdict(NamedTuple):
    """The gate's answer for one search, and the rule that decided it."""

    gated: bool
    rule: str
    target: str = ""


REASONS = {
    "directory": "a directory the graph maps code under, searched before any graph query",
    "not-a-search": "not a search",
    "stdin": "reads stdin or a pipe, not the tree",
    "unplaced": "aimed at a place only the shell can resolve, such as `$DIR` or a `cd` to one",
    "non-source-names": "can only match files no graph anchors",
    "outside-mapped-code": "outside the code the graph maps",
    "named-files": "names its files",
    "no-anchors-below": "the graph anchors nothing there",
}


def _shown(path: Path, proj: Path) -> str:
    try:
        rel = path.resolve().relative_to(proj).as_posix()
    except (ValueError, OSError):
        return str(path)
    return rel if rel not in {"", "."} else "the repo root"


def targets_verdict(
    targets: list[Path], proj: Path, root: str = "", covers: Iterable[str] = (),
    exempt: Iterable[str] = (), graph: Path | None = None,
) -> Verdict:
    """The one scope rule both Grep/Glob and a shell search follow.

    Gated only when some target is a directory that reaches mapped code and, when
    the graph is known, holds at least one anchored file. A named file is a
    located search whether or not the graph anchors it, and whether or not it
    exists yet: `cp a b && grep x b` reaches the hook before `b` does.
    """
    covers, exempt = list(covers), list(exempt)
    inside = [t for t in targets if reaches_mapped_code(t, proj, root, covers, exempt)]
    if not inside:
        return Verdict(False, "outside-mapped-code")
    dirs = [t for t in inside if t.is_dir()]
    if not dirs:
        return Verdict(False, "named-files")
    if graph is not None:
        dirs = [d for d in dirs if anchors_under(graph, root_relative(d, proj, root) or "")]
        if not dirs:
            return Verdict(False, "no-anchors-below")
    return Verdict(True, "directory", _shown(dirs[0], proj))


def shell_search_verdict(
    command: str, proj: Path, cfg: dict[str, object] | None = None, root: str = "",
    covers: Iterable[str] = (), exempt: Iterable[str] = (), graph: Path | None = None,
    cwd: Path | None = None,
) -> Verdict:
    """Is this shell command a search of THIS repo's mapped code with no file named yet?

    The same rule Grep/Glob follow, read from a command string —

      * `cd other-repo && grep -r x .`  another repo, which this graph cannot
                                        answer for;
      * `cat f | grep x`                reads a pipe, never the tree;
      * `grep x pyproject.toml`         names one file;
      * `cp a b && grep x b`            names one file, created by the `cp`;
      * `find . -name Info.plist`       can only find files no graph anchors;
      * `grep -A 5 x f.py`, `grep x $F` a flag's value or a shell variable is
                                        not a directory;
      * `rg x`, `grep -r x`             no operand: the working directory.
    """
    parsed = parse_shell_search(command, proj, cwd)
    if parsed is None:
        return Verdict(False, "not-a-search")
    clauses = [c for c in parsed.clauses if not clause_is_exempt(c, cfg or DEFAULTS)]
    targets = [t for c in clauses for t in c.targets]
    if not targets:
        if any(c.unplaced for c in clauses):
            return Verdict(False, "unplaced")
        return Verdict(False, "stdin" if clauses else "non-source-names")
    return targets_verdict(targets, proj, root, covers, exempt, graph)


def shell_search_is_gated(
    command: str, proj: Path, cfg: dict[str, object] | None = None, root: str = "",
    covers: Iterable[str] = (), exempt: Iterable[str] = (), graph: Path | None = None,
    cwd: Path | None = None,
) -> bool:
    return shell_search_verdict(command, proj, cfg, root, covers, exempt, graph, cwd).gated


def other_clauses(command: str) -> list[str]:
    """The clauses of a shell command that are not searches, `cd`s, or pipe readers.

    A hook can only allow or deny a whole call, so a denied `echo x >> f && grep y .`
    also refuses the append. These are the parts the denial must name.
    """
    out: list[str] = []
    for clause, piped in _split_clauses(strip_heredocs(command)):
        toks = _tokens(clause)
        if piped or not toks:
            continue
        word, rest = _search_word(toks)
        if word in _SEARCH_WORDS or word in _CD_WORDS:
            continue
        if word == "git" and _git_subcommand(rest, None)[0] == "grep":
            continue
        out.append(clause)
    return out


def is_shell_search(command: str) -> bool:
    return bool(_SHELL_SEARCH.search(strip_heredocs(command)))

"""The commit-time graph check: a hook endpoint the server answers itself. Stdlib only.

Claude Code calls `kg_hook_commit_check` from a `PostToolUse` hook of type
`mcp_tool` after a shell command that runs git. The tool is not in the catalog:
a hook names it, a model never sees it. It finds the commits the command made,
compares the files they changed with the graph, and returns the hook's JSON as
text: `additionalContext` that lists source no node covers, deleted source the
graph still anchors on, and mapped source that changed since the graph was
updated. It is advisory. Any failure answers `{}`, so the hook never blocks.

The hook runs after the command because an agent stages and commits in one
command (`git add -A && git commit`, `git commit -am`), and a hook that runs
first reads an empty index.

The commits a command made are the newest entries `git commit` wrote to the
HEAD reflog (`commit:`, and the initial, amend and merge forms), at most `RECENT`
seconds old, that this server has not reported. A failed `git commit` writes no
entry and reports nothing, and commits a rebase or a pull brought in are not
counted. Several commits from one command are checked as one range, from what
HEAD was before them. A repository's first commit is compared with the empty
tree. A repository with no reflog falls back to the commit at HEAD, by its
committer time. Commits are marked reported once the comparison has run, so a
check that fails is tried again on the next git command.

The comparison is `git-hooks/kg_pre_push.py`, loaded from the plugin folder, so
this check, the git hooks and the push gate share one rule. That module reads
git in one working directory at a time, so checks run one at a time.

`commit_dirs` reads the command for `git -C <dir> commit` and for `cd <dir>`
before a commit, and falls back to the session folder when it cannot read the
command. It does not follow `--git-dir`, `--work-tree`, or a commit made inside
a script the command runs.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import shim

logger = logging.getLogger("codebase_kg.commit_check")

TOOL = "kg_hook_commit_check"
CHECKER = shim.MCP_DIR.parent / "git-hooks" / "kg_pre_push.py"
RECENT = 120.0
MAX_COMMITS = 20
LIST_CAP = 20
REPORTED_CAP = 256
OFF = frozenset({"0", "false", "no", "off"})

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = frozenset(";&|()")
_CD = frozenset({"cd", "pushd", "set-location", "push-location", "sl"})
_GIT_OPTS_WITH_VALUE = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_LOOSE_COMMIT = re.compile(r"\bgit\b.*\bcommit\b", re.DOTALL)
_COMMIT_ENTRY = re.compile(r"commit( \((initial|amend|merge)\))?:")
_SELECTOR_TIME = re.compile(r"@\{(\d+)\}")
_MAX_DEPTH = 2

_lock = threading.Lock()
_checker: Any = None
_reported: OrderedDict[str, None] = OrderedDict()


def _load_checker() -> Any:
    """`git-hooks/kg_pre_push.py` as a module, compiled in memory so no `__pycache__` lands in the plugin folder."""
    global _checker
    if _checker is None:
        module = types.ModuleType("kg_pre_push")
        module.__file__ = str(CHECKER)
        exec(compile(CHECKER.read_text(encoding="utf-8"), str(CHECKER), "exec"), module.__dict__)  # noqa: S102
        _checker = module
    return _checker


def _program(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="".join(sorted(_SEPARATORS)))
    lexer.whitespace_split = True
    lexer.commenters = ""
    # Backslashes stay literal, so a Windows path keeps its separators.
    lexer.escape = ""
    return list(lexer)


def _git_commit(args: list[str], cwd: Path) -> Path | None:
    """The folder `git <args>` commits in, or None when it is not a commit."""
    i = 0
    while i < len(args):
        token = args[i]
        if token == "-C" and i + 1 < len(args):
            cwd = cwd / Path(args[i + 1]).expanduser()
            i += 2
        elif token in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return cwd if token == "commit" and "--dry-run" not in args[i + 1:] else None
    return None


def _scan(command: str, cwd: Path, depth: int) -> list[Path]:
    found: list[Path] = []
    segment: list[str] = []
    for token in [*_tokens(command), ";"]:
        if not (token and set(token) <= _SEPARATORS):
            segment.append(token)
            continue
        words = segment
        segment = []
        while words and _ASSIGN.match(words[0]):
            words = words[1:]
        if not words:
            continue
        program, rest = _program(words[0]), words[1:]
        if program in _CD and rest:
            cwd = cwd / Path(rest[0]).expanduser()
        elif program == "git":
            target = _git_commit(rest, cwd)
            if target is not None:
                found.append(target)
        elif depth < _MAX_DEPTH:
            for inner in rest:
                if " " in inner and "git" in inner and "commit" in inner:
                    found += _scan(inner, cwd, depth + 1)
    return found


def commit_dirs(command: str, cwd: Path) -> list[Path]:
    """The folders the command's `git commit` calls run in, without repeats."""
    try:
        found = _scan(command, cwd, 0)
    except ValueError:
        found = [cwd] if _LOOSE_COMMIT.search(command) else []
    return list(dict.fromkeys(found))


def _new_commits(checker: Any, now: float) -> tuple[list[str], str | None] | None:
    """The commits `git commit` just made, newest first, and what HEAD was before them.

    Read from the HEAD reflog: the newest entries that `git commit` wrote, at most
    `RECENT` seconds old and not yet reported. A rebase, pull, cherry-pick or
    checkout writes another kind of entry and ends the run, so the commits those
    brought in are never counted. Committer time cannot tell them apart: a rebase
    rewrites it.
    """
    log = checker._git(
        "log", "-g", f"--max-count={MAX_COMMITS + 1}", "--date=unix", "--format=%H%x09%gd%x09%P%x09%gs", "HEAD",
    )
    if not log.strip():
        return _head_only(checker, now)
    fresh: list[str] = []
    base: str | None = None
    for line in log.splitlines():
        sha, selector, parents, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        stamp = _SELECTOR_TIME.search(selector)
        made_here = (
            _COMMIT_ENTRY.match(subject) is not None and stamp is not None
            and now - int(stamp.group(1)) <= RECENT and sha not in _reported and len(fresh) < MAX_COMMITS
        )
        if not made_here:
            base = sha if fresh else None
            break
        fresh.append(sha)
        base = None if subject.startswith("commit (initial)") else (parents.split() or [None])[0]
    return (fresh, base) if fresh else None


def _head_only(checker: Any, now: float) -> tuple[list[str], str | None] | None:
    """The commit at HEAD when it is recent and unreported, for a repository that keeps no reflog."""
    parts = checker._git("log", "--max-count=1", "--format=%H %ct %P", "HEAD").split()
    if len(parts) < 2 or not parts[1].isdigit() or now - int(parts[1]) > RECENT or parts[0] in _reported:
        return None
    return [parts[0]], parts[2] if len(parts) > 2 else None


def _remember(commits: list[str]) -> None:
    for sha in commits:
        _reported[sha] = None
    while len(_reported) > REPORTED_CAP:
        _reported.popitem(last=False)


def _block(lines: list[str], heading: str, marker: str, files: list[str]) -> None:
    if not files:
        return
    lines.append(heading)
    lines += [f"  {marker} {path}" for path in files[:LIST_CAP]]
    if len(files) > LIST_CAP:
        lines.append(f"  ... and {len(files) - LIST_CAP} more")


def message(findings: Any, graph_rel: str, commits: list[str]) -> str:
    """The context the agent reads: what the commits left out of the graph and what to do next."""
    what = f"commit {commits[0][:9]}" if len(commits) == 1 else f"the last {len(commits)} commits"
    lines = [f"codebase-kg: {what} changed code that {graph_rel} does not describe yet."]
    _block(lines, "Deleted files the graph still anchors on:", "-", findings.deleted)
    _block(lines, "Source files no node covers:", "+", findings.unmapped)
    _block(lines, "Mapped files changed since the graph was updated:", "~", findings.drifted)
    lines.append(
        f"Run /codebase-kg:refresh, then commit {graph_rel} in a follow-up commit before you push. "
        "Do this without asking the user. This check is advisory and blocked nothing."
    )
    return "\n".join(lines)


def _check_repo(checker: Any, directory: Path, now: float) -> str | None:
    """The message for the commits just made in `directory`'s repository, or None. Caller holds `_lock`."""
    checker.GIT_CWD = str(directory)
    top = checker._git("rev-parse", "--show-toplevel").strip()
    if not top:
        return None
    repo = Path(top)
    checker.GIT_CWD = str(repo)
    fresh = _new_commits(checker, now)
    if fresh is None:
        return None
    commits, base = fresh
    said = _findings_message(checker, repo, commits, base)
    _remember(commits)
    return said


def _findings_message(checker: Any, repo: Path, commits: list[str], base: str | None) -> str | None:
    cfg = checker.load_config(repo)
    if str(cfg.get("commit_check", "")).strip().lower() in OFF:
        return None
    graph_rel = checker.find_graph_rel(repo, cfg)
    graph = checker.read_graph(repo / graph_rel) if graph_rel else None
    if graph_rel is None or graph is None:
        return None
    if base is None:
        first = commits[-1]
        listing = checker._git("diff-tree", "--no-commit-id", "--name-status", "-r", "--root", first)
        if len(commits) > 1:
            listing += checker._git("diff", "--name-status", first, "HEAD")
    else:
        listing = checker._git("diff", "--name-status", base, "HEAD")
    changed = list(dict.fromkeys(checker.parse_name_status(listing)))
    if not changed:
        return None
    root = checker.norm_root(cfg.get("root") or graph.root)
    candidates = checker.drift_candidates(changed, root, graph_rel, graph.anchored, graph.covers, graph.exempt)
    current = checker.digests_for(candidates, ["HEAD"]) if candidates else {}
    findings = checker.analyze(
        changed, root, graph_rel, graph.anchored, graph.covers, graph.exempt, graph.baselines, current,
    )
    return message(findings, graph_rel, commits) if any(findings) else None


def check(command: str, cwd: str, now: float | None = None) -> str | None:
    """The context for the commits `command` just made, or None when there is nothing to say."""
    if not Path(cwd).is_absolute():
        return None
    directories = commit_dirs(command, Path(cwd))
    if not directories:
        return None
    messages: list[str] = []
    with _lock:
        checker = _load_checker()
        try:
            for directory in directories:
                if directory.is_dir():
                    found = _check_repo(checker, directory, time.time() if now is None else now)
                    if found:
                        messages.append(found)
        finally:
            checker.GIT_CWD = None
    return "\n\n".join(messages) or None


def call(args: Any) -> dict[str, Any]:
    """The tool result: the hook's JSON as text. `{}` when there is nothing to say or the check failed."""
    out: dict[str, Any] = {}
    try:
        command = args.get("command") if isinstance(args, dict) else None
        cwd = args.get("cwd") if isinstance(args, dict) else None
        if isinstance(command, str) and isinstance(cwd, str) and "git" in command and "commit" in command:
            context = check(command, cwd)
            if context:
                out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
    except Exception:
        logger.exception("commit check failed")
    return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}

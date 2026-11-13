"""PreToolUse hook: stop an agent's `git push` while the code graph is stale.

The pre-push git hook blocks the same push, but an agent that sees that block can
retry with `--no-verify` or a `SKIP_KG=1` prefix without anyone noticing. This
hook runs earlier, in the agent's own turn, and tells it what to do: refresh the
graph, commit it, push again. It denies on the same rule the git hook uses, so
both agree on what "stale" means.

The staleness comparison is not repeated here. It comes from
`git-hooks/kg_pre_push.py`, loaded by path, so the plugin and the vendored
checker cannot disagree.

`--no-verify` does not open the gate: it skips git hooks, not this one. The ways
through are the explicit ones, `SKIP_KG=1` and `KG_STALE_ACK=<count>`, in the
command's env prefix or in this process's environment.

Fails open. Any error allows the push, because this hook must never strand the
agent or break a tool call; the git hook is still there to block.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import as_local_path, emit_deny, run_hook, skip_kg, strip_heredocs

LIST_CAP = 20

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATOR_CHARS = set("();|&")
_REDIRECT_CHARS = set("<>")
_WRAPPERS = {
    "env", "command", "exec", "time", "nohup", "sudo", "doas",
    "timeout", "nice", "ionice", "stdbuf", "xargs", "setsid",
}
_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
_WRAPPER_OPTS_WITH_VALUE = {
    "sudo": {
        "-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U",
        "--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type",
        "--other-user", "--close-from",
    },
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "-t"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a", "-l", "-i"},
}
# Positional arguments a wrapper takes before the command: `timeout 60 git push`.
_WRAPPER_POSITIONALS = {"timeout": 1}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish"}
_COMMAND_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_POWERSHELLS = {"pwsh", "powershell"}
_PS_COMMAND_FLAG = re.compile(r"^-c(ommand)?$", re.IGNORECASE)
_CMD_FLAG = re.compile(r"^/[ck]$", re.IGNORECASE)
_GIT_OPTS_WITH_VALUE = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
_MAX_DEPTH = 3
_PS_ENV = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*", re.IGNORECASE)
_PS_CD = re.compile(r"(?<![\w-])(?:Set-Location|Push-Location|sl|pushd)(?![\w-])", re.IGNORECASE)
_PS_CONTINUATION = re.compile(r"`\r?\n")
_PS_HERE_STRING = re.compile(r"@(['\"])[ \t]*\r?\n.*?\r?\n\1@", re.DOTALL)
_PS_ESCAPED_QUOTE = re.compile(r"`([\"'])")
_COMMENT_AFTER = " \t\n;|&()<>"


class Push(NamedTuple):
    cwd: Path
    env: dict[str, str]
    deletes_only: bool = False


def _program(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def powershell_to_shell(command: str) -> str:
    """A PowerShell command rewritten so `find_pushes` can read it.

    `$env:NAME = value` becomes `export NAME=value`, `Set-Location` and its aliases
    become `cd`, a trailing backtick joins its line, and backslashes become
    slashes so Windows paths survive the POSIX lexer.
    """
    command = _PS_HERE_STRING.sub("''", command)
    command = _PS_CONTINUATION.sub(" ", command)
    command = _PS_ENV.sub(lambda m: f"export {m.group(1)}=", command)
    command = _PS_CD.sub("cd", command)
    command = command.replace("\\", "/")
    # A backtick-escaped quote is a literal quote; the POSIX lexer wants a backslash.
    return _PS_ESCAPED_QUOTE.sub(lambda m: "\\" + m.group(1), command)


def _prepare(command: str) -> str:
    """`command` with the shell's own syntax resolved, ready for the lexer.

    A `#` starts a comment only at the start of a word, so `fix#12` is kept and
    `# note` is dropped. A backslash-newline joins its lines. An unquoted newline
    or backtick becomes `;`, so each command a backtick substitution runs is its own
    segment. Quoted text is left alone.
    """
    out: list[str] = []
    quote = ""
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = ""
        elif c == "\\" and nxt in ("\n", "\r"):
            i += 3 if command[i + 1 : i + 3] == "\r\n" else 2
            continue
        elif c == "\\":
            out.append(c + nxt)
            i += 2
            continue
        elif quote == '"':
            out.append(c)
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (not out or out[-1][-1] in _COMMENT_AFTER):
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif c in "\n`":
            out.append(" ; ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands at `&&`, `||`, `;`, `|`, `&`.

    Heredoc bodies are data and are dropped first. Raises `ValueError` on
    unbalanced quotes; the caller treats that as no push.
    """
    command = strip_heredocs(command)
    lex = shlex.shlex(_prepare(command), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lex:
        chars = set(token)
        if chars <= _SEPARATOR_CHARS:
            segments.append([])
        elif chars & _REDIRECT_CHARS and chars <= _SEPARATOR_CHARS | _REDIRECT_CHARS:
            continue
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _deletes_only(args: list[str]) -> bool:
    """True when every ref `git push <args>` names is a deletion.

    `args` starts after `push`. A deletion publishes no commits, so there is no
    code for a stale graph to misdescribe.
    """
    if "--delete" in args or "-d" in args:
        return True
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in _PUSH_OPTS_WITH_VALUE:
            i += 1
        elif not args[i].startswith("-"):
            positional.append(args[i])
        i += 1
    refspecs = positional[1:]
    return bool(refspecs) and all(spec.startswith(":") for spec in refspecs)


def _pushes_nothing(args: list[str]) -> bool:
    """True when `git push <args>` is a dry run or a help request, which sends nothing.

    `args` starts after `push`. The value of an option such as `-o` is skipped, and
    short flags may be bundled (`-fn`).
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return False
        if arg in ("--dry-run", "--help", "-h", "-n"):
            return True
        if arg in _PUSH_OPTS_WITH_VALUE:
            i += 1
        elif arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:].split("o", 1)[0]:
            return True
        i += 1
    return False


def _git_push(args: list[str], cwd: Path) -> tuple[Path, bool] | None:
    """The directory `git <args>` pushes from and whether it only deletes refs,
    or None when it is not a push.

    `args` starts after `git`. Global options come first: `-C <dir>` moves the
    working directory, `-c <k=v>` and the `--git-dir` family take a value, and
    any other leading dash is a flag.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "-C" and i + 1 < len(args):
            cwd = cwd / as_local_path(args[i + 1]).expanduser()
            i += 2
        elif token in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            rest = args[i + 1 :]
            if token == "subtree" and rest[:1] == ["push"]:
                return cwd, False
            if token != "push" or _pushes_nothing(rest):
                return None
            return cwd, _deletes_only(rest)
    return None


def _inner_command(prog: str, rest: list[str]) -> str | None:
    """The command text a shell, `eval`, `cmd /c` or `pwsh -c` runs, or None.

    `rest` is what follows the program name. PowerShell text is rewritten to the
    POSIX form the lexer reads.
    """
    if prog == "eval":
        return " ".join(rest)
    if prog in _SHELLS:
        flag = next((k for k, t in enumerate(rest[:-1]) if _COMMAND_FLAG.match(t)), None)
        return None if flag is None else rest[flag + 1]
    if prog in _POWERSHELLS:
        flag = next((k for k, t in enumerate(rest[:-1]) if _PS_COMMAND_FLAG.match(t)), None)
        return None if flag is None else powershell_to_shell(" ".join(rest[flag + 1 :]))
    if prog == "cmd":
        flag = next((k for k, t in enumerate(rest[:-1]) if _CMD_FLAG.match(t)), None)
        return None if flag is None else " ".join(rest[flag + 1 :])
    return None


def find_pushes(
    command: str, cwd: Path, env: dict[str, str] | None = None, depth: int = 0
) -> list[Push]:
    """Every `git push` in `command`, with the directory and env each one runs in.

    Follows `cd <dir>`, `export NAME=value`, leading `NAME=value` assignments,
    `env`/`command`/`sudo`/`timeout`-style wrappers, control keywords such as `if` and
    `then`, backtick substitutions, and `bash -c '<command>'` (also `-lc`), `eval`,
    `cmd /c` and `pwsh -c`.
    """
    pushes: list[Push] = []
    exported = dict(env or {})
    for seg in _segments(command):
        i, prefix, wrapper, positionals = 0, {}, "", 0
        while i < len(seg):
            token = seg[i]
            if _ASSIGN.match(token):
                name, _, value = token.partition("=")
                prefix[name] = value
            elif _program(token) in _WRAPPERS:
                wrapper = _program(token)
                positionals = _WRAPPER_POSITIONALS.get(wrapper, 0)
            elif token in _KEYWORDS:
                pass
            elif wrapper and token.startswith("-"):
                if token in _WRAPPER_OPTS_WITH_VALUE.get(wrapper, ()):
                    i += 1
            elif wrapper and positionals:
                positionals -= 1
            else:
                break
            i += 1
        if i >= len(seg):
            continue
        prog, rest = _program(seg[i]), seg[i + 1 :]
        if prog in ("cd", "pushd") and rest:
            cwd = cwd / as_local_path(rest[0]).expanduser()
        elif prog == "export":
            for token in rest:
                if _ASSIGN.match(token):
                    name, _, value = token.partition("=")
                    exported[name] = value
        elif prog == "git":
            push = _git_push(rest, cwd)
            if push is not None:
                pushes.append(Push(push[0], {**exported, **prefix}, push[1]))
        elif depth < _MAX_DEPTH:
            inner = _inner_command(prog, rest)
            if inner is not None:
                pushes += find_pushes(inner, cwd, {**exported, **prefix}, depth + 1)
    return pushes


def _repo_root(directory: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return None
    return Path(out) if out else None


def _load_checker():
    """`git-hooks/kg_pre_push.py`, imported from the plugin's own folder."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "git-hooks"))
    import kg_pre_push

    return kg_pre_push


def stale_files(repo: Path) -> tuple[list[str], str] | None:
    """Repo-relative stale mapped files and the graph path, or None.

    None means nothing to gate: no graph, or an unreadable one. The comparison is
    the pre-push hook's, against `HEAD`. The checker reads git from the working
    directory, so this moves there first.
    """
    checker = _load_checker()
    os.chdir(repo)
    cfg = checker.load_config(repo)
    graph_rel = checker.find_graph_rel(repo, cfg)
    if graph_rel is None:
        return None
    graph = checker.read_graph(repo / graph_rel)
    if graph is None:
        return None
    root = checker.norm_root(cfg.get("root") or graph.root)
    split = checker.repo_staleness(graph, root, ["HEAD"])
    return [checker._root_to_rel(p, root) for p in split.stale], graph_rel


def allowed_by_env(push_env: dict[str, str], count: int) -> bool:
    """SKIP_KG, or a KG_STALE_ACK naming exactly `count`, in the command or here."""
    for source in (push_env, os.environ):
        if skip_kg(source):
            return True
        ack = (source.get("KG_STALE_ACK") or "").strip()
        if ack.isdigit() and int(ack) == count:
            return True
    return False


def deny_reason(stale: list[str], graph_rel: str) -> str:
    shown = "\n".join(f"  ~ {p}" for p in stale[:LIST_CAP])
    more = f"\n  ... and {len(stale) - LIST_CAP} more" if len(stale) > LIST_CAP else ""
    return (
        f"codebase-kg: push blocked. {len(stale)} mapped file(s) no longer match "
        f"{graph_rel}:\n{shown}{more}\n\n"
        "Refresh the graph, then push again:\n"
        "1. Run /codebase-kg:refresh.\n"
        f"2. Commit {graph_rel}.\n"
        "3. Run git push again.\n\n"
        f"To accept this drift on purpose, prefix the push: KG_STALE_ACK={len(stale)} git push ... "
        "--no-verify does not skip this check."
    )


def _run(data: dict[str, object]) -> None:
    tool = data.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        return
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or "git" not in command or "push" not in command:
        return
    if skip_kg():
        return
    cwd = data.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    try:
        pushes = find_pushes(powershell_to_shell(command) if tool == "PowerShell" else command, base)
    except ValueError:
        return
    checked: dict[Path, tuple[list[str], str] | None] = {}
    for push in pushes:
        if push.deletes_only or skip_kg(push.env):
            continue
        repo = _repo_root(push.cwd)
        if repo is None:
            continue
        if repo not in checked:
            checked[repo] = stale_files(repo)
        result = checked[repo]
        if result is None or not result[0]:
            continue
        stale, graph_rel = result
        if allowed_by_env(push.env, len(stale)):
            continue
        emit_deny(deny_reason(stale, graph_rel))
        return


def main() -> None:
    run_hook(_run)


if __name__ == "__main__":
    main()

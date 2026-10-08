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

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

LIST_CAP = 20

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATOR_CHARS = set("();|&")
_REDIRECT_CHARS = set("<>")
_WRAPPERS = {"env", "command", "exec", "time", "nohup", "sudo"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_GIT_OPTS_WITH_VALUE = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_MAX_DEPTH = 3


class Push(NamedTuple):
    cwd: Path
    env: dict[str, str]


def _program(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands at `&&`, `||`, `;`, `|`, `&`.

    Raises `ValueError` on unbalanced quotes; the caller treats that as no push.
    """
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
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


def _git_push_dir(args: list[str], cwd: Path) -> Path | None:
    """The directory `git <args>` pushes from, or None when it is not a push.

    `args` starts after `git`. Global options come first: `-C <dir>` moves the
    working directory, `-c <k=v>` and the `--git-dir` family take a value, and
    any other leading dash is a flag.
    """
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
            return cwd if token == "push" else None
    return None


def find_pushes(
    command: str, cwd: Path, env: dict[str, str] | None = None, depth: int = 0
) -> list[Push]:
    """Every `git push` in `command`, with the directory and env each one runs in.

    Follows `cd <dir>`, `export NAME=value`, leading `NAME=value` assignments,
    `env`/`command`/`sudo`-style wrappers, and `bash -c '<command>'`.
    """
    pushes: list[Push] = []
    exported = dict(env or {})
    for seg in _segments(command):
        i, prefix = 0, {}
        while i < len(seg):
            token = seg[i]
            if _ASSIGN.match(token):
                name, _, value = token.partition("=")
                prefix[name] = value
            elif _program(token) in _WRAPPERS or (
                token.startswith("-") and i > 0 and _program(seg[i - 1]) == "env"
            ):
                pass
            else:
                break
            i += 1
        if i >= len(seg):
            continue
        prog, rest = _program(seg[i]), seg[i + 1 :]
        if prog in ("cd", "pushd") and rest:
            cwd = cwd / Path(rest[0]).expanduser()
        elif prog == "export":
            for token in rest:
                if _ASSIGN.match(token):
                    name, _, value = token.partition("=")
                    exported[name] = value
        elif prog == "git":
            run_dir = _git_push_dir(rest, cwd)
            if run_dir is not None:
                pushes.append(Push(run_dir, {**exported, **prefix}))
        elif prog in _SHELLS and depth < _MAX_DEPTH and "-c" in rest[:-1]:
            inner = rest[rest.index("-c") + 1]
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


def _truthy(value: str | None) -> bool:
    return bool((value or "").strip())


def allowed_by_env(push_env: dict[str, str], count: int) -> bool:
    """SKIP_KG, or a KG_STALE_ACK naming exactly `count`, in the command or here."""
    for source in (push_env, os.environ):
        if _truthy(source.get("SKIP_KG")):
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


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _run(data: dict[str, object]) -> None:
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or "git" not in command or "push" not in command:
        return
    if _truthy(os.environ.get("SKIP_KG")):
        return
    cwd = data.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    try:
        pushes = find_pushes(command, base)
    except ValueError:
        return
    checked: dict[Path, tuple[list[str], str] | None] = {}
    for push in pushes:
        if _truthy(push.env.get("SKIP_KG")):
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
        _deny(deny_reason(stale, graph_rel))
        return


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            _run(data)
    except Exception:  # noqa: BLE001
        # Fail open: a gate that errors must let the push through to the git hook.
        return


if __name__ == "__main__":
    main()

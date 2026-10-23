"""Advisory PostToolUse hook — nudge to refresh the code graph after source edits.

Fires on Edit/Write, and on Codex's `apply_patch`. Two signals, in order of strength:

1. **The edited file is not in the graph at all.** No node anchors on it, so the
   map has a hole exactly where you are working. That is a fact, not a guess, so
   it is worth saying the first time it happens.
2. **Enough anchored files have changed since the graph was last touched.** A
   weaker heuristic for the descriptions drifting, kept as the periodic nudge.

It is **advisory only**: it never blocks, never edits, and any error exits
silently so an edit is never broken.

State (a per-project counter plus the set of files already reported) lives in the
OS temp dir — nothing is written into the user's repo.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (
    as_int,
    find_graph,
    graph_meta,
    is_anchored,
    is_source_file,
    load_config,
    load_declared_coverage,
    norm_path,
    norm_root,
    project_dir,
    read_state,
    run_hook,
    state_path,
    write_state,
)

_EDIT_TOOLS = {"Edit", "Write"}
GRAPH_FILENAME = "code_graph.db"


def _same(a: Path, b: Path) -> bool:
    return norm_path(a) == norm_path(b)


def _state_path(proj: Path) -> Path:
    return state_path("codebase-kg-hook", norm_path(proj))


def _graph_stamp(graph: Path) -> int | None:
    try:
        return graph.stat().st_mtime_ns
    except OSError:
        return None


def _write(proj: Path, state: dict[str, object]) -> None:
    write_state(_state_path(proj), state)


def _emit(message: str) -> None:
    print(json.dumps({"systemMessage": message}))


def _rel_to_root(path: Path, proj: Path, root: str) -> str | None:
    """The edited path as the graph would anchor it: relative to `root`."""
    try:
        rel = path.relative_to(proj).as_posix()
    except ValueError:
        return None
    root = norm_root(root)
    if root:
        if not rel.startswith(root + "/"):
            return None
        rel = rel[len(root) + 1 :]
    return rel


_PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Move to): (.+?)\s*$", re.MULTILINE)


def edited_paths(data: dict[str, object]) -> list[str]:
    """The files one edit call touched: Claude's `file_path`, or an `apply_patch` body.

    Codex reports every file edit as `tool_name: apply_patch` with the patch text in
    `tool_input.command`. Its paths are relative to the session cwd unless absolute.
    """
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    if tool in _EDIT_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("path") or ""
        return [fp] if fp and isinstance(fp, str) else []
    if tool == "apply_patch":
        patch = tool_input.get("command")
        if not isinstance(patch, str):
            return []
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        return [str(Path(cwd) / m) if cwd else m for m in _PATCH_FILE.findall(patch)]
    return []


def _run(data: dict[str, object]) -> None:
    paths = edited_paths(data)
    if not paths:
        return

    proj = project_dir(data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    cfg = load_config(proj)
    if not cfg.get("post_edit_nudge", True):
        return

    graph = find_graph(proj, cfg)
    if graph is None:
        return  # no graph in this repo → nothing to keep in sync

    # `root` comes from the committed graph meta; .local.md may override.
    if not cfg.get("root"):
        cfg["root"] = graph_meta(graph, "root")
    load_declared_coverage(graph, cfg)

    for fp in paths:
        if _check(fp, proj, cfg, graph):
            return


def _check(fp: str, proj: Path, cfg: dict[str, object], graph: Path) -> bool:
    """Nudge for one edited file. True when a message was sent, so one call sends at most one."""
    edited = Path(fp).resolve()
    state = read_state(_state_path(proj))
    stamp = _graph_stamp(graph)

    # Touching the graph itself clears the pending counter.
    if _same(edited, graph) or edited.name == GRAPH_FILENAME:
        _write(proj, {"pending": 0, "reported": [], "graph_stamp": stamp})
        return False

    if not is_source_file(edited, proj, cfg):
        return False

    # A refresh writes the graph through `kg_upsert_node` or the CLI, not through
    # an Edit, so a changed file is the only sign the counter's edits are covered.
    if state.get("graph_stamp") not in (None, stamp):
        state = {}

    rel = _rel_to_root(edited, proj, str(cfg.get("root") or ""))
    reported = [str(r) for r in state.get("reported", []) if isinstance(r, str)]
    pending = as_int(state.get("pending"), 0) + 1

    # Signal 1: this file is not in the map. Report once per file per session.
    if rel is not None and rel not in reported and not is_anchored(graph, rel):
        reported.append(rel)
        _write(proj, {"pending": pending, "reported": reported[-50:], "graph_stamp": stamp})
        _emit(
            f"codebase-kg: no node in {graph.name} anchors on {rel}. "
            f"Run /codebase-kg:refresh to add it, so the graph keeps answering "
            f"'where does this live?' correctly (advisory, never blocking)."
        )
        return True

    _write(proj, {"pending": pending, "reported": reported, "graph_stamp": stamp})

    # Signal 2: enough mapped files have changed that descriptions may have drifted.
    # `or 5` before the coercion, not after: a falsy `nudge_every` has always
    # meant "unset", and this line is fixing a crash, not redefining the config.
    every = max(1, as_int(cfg.get("nudge_every") or 5, 5))
    if pending % every == 0:
        _emit(
            f"codebase-kg: {pending} source edit(s) since {graph.name} was last rebuilt. "
            f"At a stopping point, run /codebase-kg:refresh so the graph stays in sync "
            f"(advisory, never blocking)."
        )
        return True
    return False


def main() -> None:
    run_hook(_run)


if __name__ == "__main__":
    main()

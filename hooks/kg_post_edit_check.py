"""Advisory PostToolUse hook — nudge to refresh the code graph after source edits.

Fires on Edit/Write/MultiEdit. Two signals, in order of strength:

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

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import (  # noqa: E402
    find_graph,
    graph_meta,
    is_anchored,
    is_source_file,
    load_config,
    project_dir,
)

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
GRAPH_FILENAME = "code_graph.db"


def _as_int(value: object, default: int) -> int:
    """A config or state value as an int, falling back rather than raising.

    `nudge_every` is user-typed and `pending` comes off disk, so both reach
    `int()` as arbitrary objects. A bare `int("evry")` raises, `main` swallows
    it, and the hook goes silent for that repo forever with no error anywhere —
    the same invisible-disable that the frontmatter comment-strip exists to
    prevent. A typo should cost the setting, not the feature.
    """
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _same(a: Path, b: Path) -> bool:
    return _norm(a) == _norm(b)


def _state_path(proj: Path) -> Path:
    h = hashlib.sha1(_norm(proj).encode("utf-8")).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "codebase-kg-hook"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def _read_state(proj: Path) -> dict[str, object]:
    try:
        data = json.loads(_state_path(proj).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(proj: Path, state: dict[str, object]) -> None:
    try:
        _state_path(proj).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _emit(message: str) -> None:
    print(json.dumps({"systemMessage": message, "suppressOutput": False}))


def _rel_to_root(path: Path, proj: Path, root: str) -> str | None:
    """The edited path as the graph would anchor it: relative to `root`."""
    try:
        rel = path.relative_to(proj).as_posix()
    except ValueError:
        return None
    root = root.replace("\\", "/").strip("/")
    if root:
        if not rel.startswith(root + "/"):
            return None
        rel = rel[len(root) + 1 :]
    return rel


def _run(data: dict[str, object]) -> None:
    if data.get("tool_name") not in _EDIT_TOOLS:
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    if not fp or not isinstance(fp, str):
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

    edited = Path(fp).resolve()
    state = _read_state(proj)

    # Touching the graph itself clears the pending counter.
    if _same(edited, graph) or edited.name == GRAPH_FILENAME:
        _write_state(proj, {"pending": 0, "reported": []})
        return

    if not is_source_file(edited, proj, cfg):
        return

    rel = _rel_to_root(edited, proj, str(cfg.get("root") or ""))
    reported = [str(r) for r in state.get("reported", []) if isinstance(r, str)]
    pending = _as_int(state.get("pending"), 0) + 1

    # Signal 1: this file is not in the map. Report once per file per session.
    if rel is not None and rel not in reported and not is_anchored(graph, rel):
        reported.append(rel)
        _write_state(proj, {"pending": pending, "reported": reported[-50:]})
        _emit(
            f"codebase-kg: no node in {graph.name} anchors on {rel}. "
            f"Run /codebase-kg:refresh to add it, so the graph keeps answering "
            f"'where does this live?' correctly (advisory, never blocking)."
        )
        return

    _write_state(proj, {"pending": pending, "reported": reported})

    # Signal 2: enough mapped files have changed that descriptions may have drifted.
    # `or 5` before the coercion, not after: a falsy `nudge_every` has always
    # meant "unset", and this line is fixing a crash, not redefining the config.
    every = max(1, _as_int(cfg.get("nudge_every") or 5, 5))
    if pending % every == 0:
        _emit(
            f"codebase-kg: {pending} source edit(s) since {graph.name} was last rebuilt. "
            f"At a stopping point, run /codebase-kg:refresh so the graph stays in sync "
            f"(advisory, never blocking)."
        )


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            _run(data)
    except Exception:
        # An advisory hook must never break an edit. Swallow everything.
        return


if __name__ == "__main__":
    main()

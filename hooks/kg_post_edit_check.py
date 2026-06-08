"""Advisory PostToolUse hook — nudge to refresh the KG after source edits.

Fires on Edit/Write/MultiEdit. When source files under the repo's `root` change
without the `KNOWLEDGE_GRAPH.md` being touched, it surfaces a quiet reminder to
run `/codebase-kg:refresh`. It is **advisory only**: it never blocks, never edits,
and any error exits silently so an edit is never broken.

State (a per-project pending-edit counter) lives in the OS temp dir — nothing is
written into the user's repo.
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
    find_kg,
    is_source_file,
    load_config,
    project_dir,
)

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _same(a: Path, b: Path) -> bool:
    return _norm(a) == _norm(b)


def _state_path(proj: Path) -> Path:
    h = hashlib.sha1(_norm(proj).encode("utf-8")).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "codebase-kg-hook"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def _read_pending(proj: Path) -> int:
    try:
        return int(json.loads(_state_path(proj).read_text(encoding="utf-8")).get("pending", 0))
    except (OSError, ValueError):
        return 0


def _write_pending(proj: Path, pending: int) -> None:
    try:
        _state_path(proj).write_text(json.dumps({"pending": pending}), encoding="utf-8")
    except OSError:
        pass


def _emit(message: str) -> None:
    print(json.dumps({"systemMessage": message, "suppressOutput": False}))


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

    kg = find_kg(proj, cfg)
    if kg is None:
        return  # no KG in this repo → nothing to keep in sync

    edited = Path(fp).resolve()

    # Touching the KG (or a peer KG) clears the pending counter.
    if _same(edited, kg) or edited.name == "KNOWLEDGE_GRAPH.md":
        _write_pending(proj, 0)
        return

    if not is_source_file(edited, proj, cfg, kg):
        return

    pending = _read_pending(proj) + 1
    _write_pending(proj, pending)

    every = max(1, int(cfg.get("nudge_every", 5) or 5))
    if pending >= every and pending % every == 0:
        _emit(
            f"codebase-kg: {pending} source edit(s) since {kg.name} was last touched. "
            f"At a stopping point, run /codebase-kg:refresh so the KG stays in sync — "
            f"update the affected nodes, not just the header (advisory, never blocking)."
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

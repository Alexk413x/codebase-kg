"""A pool worker: runs the shared server's tool calls, one at a time. Stdlib only.

`pool.Pool` starts it as `python -I -S -c <entry> <src>`. It reads one JSON
request per line on stdin, `{"id", "tool", "args", "graph"}`, and writes one
JSON reply per line on stdout: `{"id", "ok": true, "result"}`,
`{"id", "ok": false, "error"}`, or, for a write the graph refused,
`{"id", "ok": false, "refused": {"ok": false, "written": false, "error"}}`.
Stdin EOF ends it, so a worker never outlives the server that holds its pipe.

Reads dispatch through `query.TOOLS`, the table `kg_cli.py query` runs, so a
lookup returns the same JSON from the CLI and the server. Writes go through
`edits`. The server resolves each call's graph from the session and sends its
absolute path; a worker never looks for a graph itself. The graph opens per
call and closes again. The server sends one write per graph at a time.

With `max_workers` 0 the server calls `run` in its own process instead.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import query
from .store import CodeGraph, StoreError

Write = Callable[[Path, dict[str, Any]], dict[str, Any]]


def _writes() -> dict[str, Write]:
    from . import edits

    return {
        "kg_upsert_node": lambda p, a: edits.upsert_node(p, a["nodes"]),
        "kg_delete_node": lambda p, a: edits.delete_node(
            p, a["ids"], dry_run=a.get("dry_run", True), cascade_inbound=a.get("cascade_inbound", False)
        ),
        "kg_add_link": lambda p, a: edits.add_link(p, a["node_id"], a["target"], a.get("kind", "")),
        "kg_remove_link": lambda p, a: edits.remove_link(p, a["node_id"], a["target"]),
        "kg_add_reference": lambda p, a: edits.add_reference(
            p, a["node_id"], a["url"], a.get("kind", ""), a.get("title", ""), a.get("path"), a.get("symbol")
        ),
        "kg_remove_reference": lambda p, a: edits.remove_reference(
            p, a["node_id"], a["url"], a.get("path"), a.get("symbol")
        ),
    }


WRITE_TOOLS = frozenset({
    "kg_upsert_node", "kg_delete_node", "kg_add_link", "kg_remove_link",
    "kg_add_reference", "kg_remove_reference",
})


def refusal(error: str) -> dict[str, Any]:
    """A rejected edit: data an agent can act on. `written: false` means the file is byte-identical."""
    return {"ok": False, "written": False, "error": error}


def _write(name: str, graph: Path, args: dict[str, Any]) -> dict[str, Any]:
    # edits imports only on the first write, so a worker that only reads stays small.
    from .edits import EditError

    try:
        return {"ok": True, "result": _writes()[name](graph, args)}
    except (EditError, StoreError, FileNotFoundError) as exc:
        return {"ok": False, "refused": refusal(str(exc))}


def run(request: dict[str, Any]) -> dict[str, Any]:
    name = request.get("tool")
    graph = request.get("graph")
    if not isinstance(graph, str) or not Path(graph).is_absolute():
        return {"ok": False, "error": "the server sent no absolute graph path"}
    args = request.get("args") or {}
    if name in WRITE_TOOLS:
        return _write(str(name), Path(graph), args)
    tool = query.TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    g = CodeGraph(Path(graph))
    try:
        return {"ok": True, "result": tool(g, args)}
    finally:
        g.close()


def answer(line: bytes) -> bytes:
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("a request must be a JSON object")
    except ValueError as exc:
        reply: dict[str, Any] = {"id": None, "ok": False, "error": f"malformed request: {exc}"}
    else:
        try:
            reply = run(request)
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        reply["id"] = request.get("id")
    try:
        return json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        return json.dumps({"id": reply.get("id"), "ok": False, "error": f"unserializable result: {exc}"}).encode(
            "utf-8"
        ) + b"\n"


def main() -> None:
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        out.write(answer(line))
        out.flush()


if __name__ == "__main__":
    main()

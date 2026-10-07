"""A pool worker: runs the shared server's read tools, one call at a time.

`pool.Pool` starts it as `python -I -S -c <entry> <src>`. It reads one JSON
request per line on stdin, `{"id", "tool", "args", "graph"}`, and writes one
JSON reply per line on stdout, `{"id", "ok": true, "result"}` or
`{"id", "ok": false, "error"}`. Stdin EOF ends it, so a worker never outlives
the server that holds its pipe.

Tools dispatch through `query.TOOLS`, the table `kg_cli.py query` runs, so a
lookup returns the same JSON from the CLI, a worker and the in-process server.
The server resolves each call's graph from the session's binding and sends its
absolute path; a worker never looks for a graph itself. The graph opens per call
and closes again, as it does in the server.

Stdlib only: a worker imports no fastmcp, so it costs about a fifth of the
server's memory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import query
from .store import CodeGraph


def run(request: dict[str, Any]) -> dict[str, Any]:
    name = request.get("tool")
    tool = query.TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        return {"ok": False, "error": f"unknown read tool {name!r}"}
    graph = request.get("graph")
    if not isinstance(graph, str) or not Path(graph).is_absolute():
        return {"ok": False, "error": "the server sent no absolute graph path"}
    args = request.get("args") or {}
    g = CodeGraph(Path(graph))
    try:
        return {"ok": True, "result": tool(g, args)}
    finally:
        g.close()


def main() -> None:
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
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
        out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()


if __name__ == "__main__":
    main()

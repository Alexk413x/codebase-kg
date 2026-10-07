"""Query the code graph from a shell: query.py <tool> [json-args].

Runs one read tool and prints the same JSON the MCP tool returns. Arguments are
one JSON object, given as the second argument or on stdin, with the MCP tool's
parameter names. Stdlib only, so `mcp/launch/kg_cli.py query …` runs it without a venv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cli, tools
from .store import CodeGraph, StoreError, discover_graph

L = tools.DEFAULT_LIMIT


def _validate(g: CodeGraph, a: dict[str, Any]) -> dict[str, Any]:
    with tools.open_peer(g) as peer:
        return tools.cap_issues(tools.kg_validate(g, peer), a.get("limit", L))


TOOLS: dict[str, Callable[[CodeGraph, dict[str, Any]], dict[str, Any]]] = {
    "kg_search": lambda g, a: tools.kg_search(g, a["query"], a.get("kind")),
    "kg_node": lambda g, a: tools.kg_node(g, a["id"]),
    "kg_neighborhood": lambda g, a: tools.kg_neighborhood(
        g, a["id"], a.get("depth", 1), a.get("limit", L), a.get("offset", 0)
    ),
    "kg_find_by_kind": lambda g, a: tools.kg_find_by_kind(g, a["kind"], a.get("limit", L), a.get("offset", 0)),
    "kg_find_by_path": lambda g, a: tools.kg_find_by_path(g, a["path"], a.get("limit", L), a.get("offset", 0)),
    "kg_find_by_link": lambda g, a: tools.kg_find_by_link(g, a["target"]),
    "kg_find_by_reference": lambda g, a: tools.kg_find_by_reference(
        g, a.get("query"), a.get("kind"), a.get("limit", L), a.get("offset", 0)
    ),
    "kg_parity_gaps": lambda g, a: tools.kg_parity_gaps(g, a.get("status"), a.get("limit", L), a.get("offset", 0)),
    "kg_stats": lambda g, a: tools.kg_stats(g),
    "kg_validate": _validate,
}


def _graph_path(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("CODEBASE_KG_PATH")
    if env:
        return Path(env)
    return discover_graph()


def _fail(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}))
    return 1


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(prog="kg_cli.py query", description=__doc__)
    ap.add_argument("--graph", help="path to code_graph.db (default: $CODEBASE_KG_PATH, then walk up)")
    ap.add_argument("tool", choices=sorted(TOOLS))
    ap.add_argument("args", nargs="?", help="JSON object of arguments (default: read stdin, or none)")
    ns = ap.parse_args(argv)

    raw = ns.args if ns.args is not None else ("" if sys.stdin.isatty() else sys.stdin.read())
    try:
        call_args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return _fail(f"arguments are not JSON: {exc}")
    if not isinstance(call_args, dict):
        return _fail("arguments must be a JSON object")

    path = _graph_path(ns.graph)
    if path is None or not path.is_file():
        return _fail(f"no code graph at {path or 'knowledge/code_graph.db'}")
    try:
        g = CodeGraph(path)
    except StoreError as exc:
        return _fail(str(exc))
    try:
        result = TOOLS[ns.tool](g, call_args)
    except KeyError as exc:
        return _fail(f"missing argument {exc}")
    finally:
        g.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

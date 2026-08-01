"""Export a `code_graph.db` back to the authoring JSON. Stdlib only.

    python -m codebase_kg.export                        # knowledge/code_graph.db -> stdout
    python -m codebase_kg.export -o graph.json
    python -m codebase_kg.export path/to.db --ids feed_ranker,saved_article

This is the read half of the refresh loop: export → edit the JSON → build. It is
lossless, so a build of an unedited export reproduces the same file byte for byte.

`--ids` exports a subset, for the common case of reworking a handful of nodes
without loading a large graph into context. Note that a subset build would drop
edges to the nodes left out, so `--ids` is for inspection and for editing that is
merged back into a full document — not for rebuilding the graph from.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cli, codec
from .store import CodeGraph, StoreError

DEFAULT_SOURCE = Path("knowledge") / "code_graph.db"


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(
        prog="python -m codebase_kg.export",
        description="Export a code_graph.db to the JSON authoring format.",
    )
    ap.add_argument(
        "source", nargs="?", help=f"path to the graph (default: {DEFAULT_SOURCE})"
    )
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--ids", help="comma-separated node ids to export (default: all)")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    args = ap.parse_args(argv)

    source = Path(args.source) if args.source else DEFAULT_SOURCE
    try:
        graph = CodeGraph(source)
    except StoreError as exc:
        sys.stderr.write(f"[codebase-kg] {exc}\n")
        return 1

    try:
        if args.ids:
            wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
            nodes = graph.nodes(wanted)
            missing = sorted(set(wanted) - {n.id for n in nodes})
            if missing:
                sys.stderr.write(f"[codebase-kg] no such node(s): {', '.join(missing)}\n")
        else:
            nodes = graph.all_nodes()
        doc = codec.to_dict(graph.meta, nodes, graph.sources())
    finally:
        graph.close()

    text = json.dumps(doc, indent=args.indent, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"[codebase-kg] wrote {args.out} ({len(doc['nodes'])} nodes)")
    else:
        # Bytes, not console-encoded text: git's textconv driver reads this
        # stdout directly, and a cp1252 console cannot encode the em-dashes and
        # arrows that descriptions routinely contain.
        cli.write_out(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

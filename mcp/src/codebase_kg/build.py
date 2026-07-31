"""Build a `code_graph.db` from an authored JSON document. Stdlib only.

    python -m codebase_kg.build graph.json                  # -> knowledge/code_graph.db
    python -m codebase_kg.build graph.json -o path/to.db
    cat graph.json | python -m codebase_kg.build -

This is what `/codebase-kg:build` and `/codebase-kg:refresh` call once the agent
has derived nodes from source. Validation is deliberately loud: a rejected build
names the node and the rule it broke, so the agent can fix that node rather than
regenerate everything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codec, writer

DEFAULT_OUT = Path("knowledge") / "code_graph.db"


def _load(source: str) -> object:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


def _render(report: writer.BuildReport, target: Path) -> str:
    lines = [
        f"[codebase-kg] wrote {target}",
        f"  nodes {report.nodes}   edges {report.edges}   anchors {report.anchors}",
    ]
    if report.dropped_edges:
        lines.append(f"  dropped {len(report.dropped_edges)} dangling edge(s):")
        for e in report.dropped_edges[:10]:
            lines.append(f"      {e[0]} -> {e[1]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m codebase_kg.build",
        description="Build a committed code_graph.db from an authored JSON graph.",
    )
    ap.add_argument("source", help="path to the JSON document, or '-' for stdin")
    ap.add_argument("-o", "--out", help=f"output path (default: {DEFAULT_OUT})")
    ap.add_argument(
        "--allow-dangling",
        action="store_true",
        help="drop edges pointing at unknown nodes instead of failing",
    )
    args = ap.parse_args(argv)

    try:
        doc = _load(args.source)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[codebase-kg] cannot read {args.source}: {exc}\n")
        return 1

    try:
        meta, nodes = codec.from_dict(doc)
    except codec.DecodeError as exc:
        sys.stderr.write(f"[codebase-kg] invalid graph document: {exc}\n")
        return 1

    target = Path(args.out) if args.out else DEFAULT_OUT
    try:
        report = writer.build(
            target, meta, nodes, dangling="drop" if args.allow_dangling else "error"
        )
    except writer.BuildError as exc:
        sys.stderr.write(
            f"[codebase-kg] refused to build: {exc}\n"
            f"[codebase-kg] Nothing was written. Fix that node and run again.\n"
        )
        return 1
    except OSError as exc:
        sys.stderr.write(f"[codebase-kg] cannot write {target}: {exc}\n")
        return 1

    print(_render(report, target))
    return 0


if __name__ == "__main__":
    sys.exit(main())

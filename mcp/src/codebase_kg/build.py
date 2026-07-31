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
from .coverage import resolve_source_base

DEFAULT_OUT = Path("knowledge") / "code_graph.db"


def _load(source: str) -> object:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


def _render(report: writer.BuildReport, target: Path, base: Path | None) -> str:
    lines = [
        f"[codebase-kg] wrote {target}",
        f"  nodes {report.nodes}   edges {report.edges}   anchors {report.anchors}",
    ]
    if base is None:
        lines.append(
            "  no source tree found — anchors have no baseline, so kg_validate "
            "cannot report source drift. Pass --source-root to enable it."
        )
    else:
        lines.append(f"  hashed {report.hashed}/{report.anchors} anchor(s) against {base}")
    if report.missing_sources:
        lines.append(f"  {len(report.missing_sources)} anchor path(s) not readable:")
        for p in report.missing_sources[:10]:
            lines.append(f"      {p}")
        if len(report.missing_sources) > 10:
            lines.append(f"      … and {len(report.missing_sources) - 10} more")
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
    ap.add_argument(
        "--source-root",
        help="repo root the anchor paths resolve from (default: inferred from the "
        "output location and the graph's `root`). Each anchored file is hashed so "
        "kg_validate can later report that source changed under a description.",
    )
    ap.add_argument(
        "--rebaseline",
        action="store_true",
        help="discard recorded baselines and re-hash every anchored file. Say this "
        "only when the descriptions have actually been re-checked against the code — "
        "otherwise it silently blesses nodes nobody verified.",
    )
    ap.add_argument(
        "--no-hash",
        action="store_true",
        help="skip source hashing even when the tree is available",
    )
    args = ap.parse_args(argv)

    try:
        doc = _load(args.source)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[codebase-kg] cannot read {args.source}: {exc}\n")
        return 1

    try:
        meta, nodes, sources = codec.from_dict(doc)
    except codec.DecodeError as exc:
        sys.stderr.write(f"[codebase-kg] invalid graph document: {exc}\n")
        return 1

    target = Path(args.out) if args.out else DEFAULT_OUT
    # Resolved from the *output* location rather than the CWD, so building into
    # another repo's knowledge/ still hashes that repo's source and not this one.
    base: Path | None = None
    if not args.no_hash:
        rels = [a.path for n in nodes for a in n.anchors]
        base = resolve_source_base(
            target.resolve().parent, meta.root, rels, args.source_root
        )
        if args.source_root and base is None:
            sys.stderr.write(
                f"[codebase-kg] --source-root {args.source_root} resolves none of "
                f"the anchor paths; building without source hashes.\n"
            )
    try:
        report = writer.build(
            target,
            meta,
            nodes,
            dangling="drop" if args.allow_dangling else "error",
            source_root=base,
            sources=sources,
            rebaseline=args.rebaseline,
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

    print(_render(report, target, base))
    return 0


if __name__ == "__main__":
    sys.exit(main())

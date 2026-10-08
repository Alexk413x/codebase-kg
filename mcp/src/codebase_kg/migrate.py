"""One-time migration: `KNOWLEDGE_GRAPH.md` → `code_graph.db`. Stdlib only.

Run once per repo:

    python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md

Convert, don't regenerate. The structure in an existing graph (ids, kinds,
anchors, edges, parity) is real work and survives verbatim. What gets rewritten
is the `summary` field: ticket refs, dates and change narrative are stripped,
because they duplicated git and were the reason the file needed constant
tending.

The migration never invents text. When scrubbing cannot get a description inside
the contract it keeps the best version it can and lists the node in
`needs_rewrite`, so `/codebase-kg:refresh` can rewrite those few from source.
It is also non-destructive: the markdown file is left exactly where it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import clean, cli, markdown, writer
from .coverage import resolve_source_base
from .models import Anchor, Node

LEGACY_NAME = "KNOWLEDGE_GRAPH.md"


@dataclass
class MigrationReport:
    source: str = ""
    target: str = ""
    source_nodes: int = 0
    nodes: int = 0
    edges: int = 0
    anchors: int = 0
    hashed: int = 0
    descriptions_scrubbed: int = 0
    chars_removed: int = 0
    dropped_edges: list[dict[str, str]] = field(default_factory=list)
    normalized: list[dict[str, str]] = field(default_factory=list)
    needs_rewrite: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "source_nodes": self.source_nodes,
            "nodes": self.nodes,
            "edges": self.edges,
            "anchors": self.anchors,
            "descriptions_scrubbed": self.descriptions_scrubbed,
            "chars_removed": self.chars_removed,
            "dropped_edges": self.dropped_edges,
            "normalized": self.normalized,
            "needs_rewrite": self.needs_rewrite,
        }


def _retarget_counterpart(counterpart: str) -> str:
    """Point a `../peer/knowledge/KNOWLEDGE_GRAPH.md#id` link at the new store."""
    path, sep, node_id = counterpart.partition("#")
    if path.endswith(LEGACY_NAME):
        path = path[: -len(LEGACY_NAME)] + "code_graph.db"
    return f"{path}{sep}{node_id}" if sep else path


def _normalize_parity(node: Node, report: MigrationReport) -> None:
    """Force the parity triple into one of the three legal shapes (SCHEMA.md §8).

    The old format allowed inconsistent combinations because nothing enforced
    them; the store's CHECK constraints do, so anything that would be rejected is
    cleared here and reported rather than silently reshaped into a guess.
    """

    def drop(reason: str, *fields: str) -> None:
        """Record why, then clear the named fields."""
        report.normalized.append({"node": node.id, "issue": reason})
        for f in fields:
            setattr(node, f, None)

    p = (node.parity or "").strip().lower() or None
    node.parity = p
    if node.counterpart:
        node.counterpart = _retarget_counterpart(node.counterpart)

    if p is None:
        if node.counterpart:
            drop("counterpart set with no parity flag — counterpart dropped", "counterpart")
        if node.divergence:
            drop("divergence set with no parity flag — divergence dropped", "divergence")
        return

    if p not in {"matched", "divergent"} and not p.endswith("-only"):
        drop(f"unrecognized parity '{p}' — parity dropped", "parity", "counterpart", "divergence")
        return

    if p.endswith("-only"):
        if node.counterpart:
            drop(f"parity={p} cannot have a counterpart — counterpart dropped", "counterpart")
        if node.divergence:
            drop(f"parity={p} cannot have a divergence — divergence dropped", "divergence")
        return

    if p == "matched" and not node.counterpart:
        drop("parity=matched with no counterpart — parity dropped", "parity", "divergence")
        return

    if p == "matched" and node.divergence:
        drop("parity=matched cannot carry a divergence — divergence dropped", "divergence")
        return

    if p == "divergent" and not (node.counterpart and node.divergence):
        missing = "counterpart" if not node.counterpart else "divergence line"
        drop(
            f"parity=divergent with no {missing} — parity dropped",
            "parity",
            "counterpart",
            "divergence",
        )


def _normalize_anchors(node: Node, report: MigrationReport) -> None:
    """Drop line-number symbols (SCHEMA.md §4.1) and empty anchors."""
    kept: list[Anchor] = []
    for a in node.anchors:
        if not a.path:
            continue
        if a.symbol and a.symbol[0].isdigit():
            report.normalized.append(
                {"node": node.id, "issue": f"line-number anchor '{a}' reduced to its path"}
            )
            kept.append(Anchor(path=a.path))
            continue
        kept.append(a)
    node.anchors = kept


def convert(
    source: Path,
    target: Path,
    *,
    generated: str | None = None,
    source_root: str | Path | None = None,
) -> MigrationReport:
    """Read the markdown graph at `source` and write `target`. Returns a report."""
    report = MigrationReport(source=str(source), target=str(target))
    meta, nodes = markdown.load(source)
    report.source_nodes = len(nodes)
    if generated:
        meta.generated = generated
    if meta.counterpart:
        meta.counterpart = _retarget_counterpart(meta.counterpart)

    for node in nodes:
        original = node.description
        cleaned, leftover = clean.scrub(original)
        if cleaned != original:
            report.descriptions_scrubbed += 1
            report.chars_removed += max(0, len(original) - len(cleaned))
        node.description = cleaned
        if leftover:
            # Should not happen — scrub() removes everything it can prove is
            # history — but if it does, the node is named rather than dropped.
            report.needs_rewrite.append({"node": node.id, "problem": "; ".join(leftover)})
            node.description = ""
        if not node.kind.strip():
            report.normalized.append({"node": node.id, "issue": "no kind — set to 'unknown'"})
            node.kind = "unknown"
        _normalize_anchors(node, report)
        _normalize_parity(node, report)

    # Hash against the working tree the markdown described, so a migrated graph
    # starts with a real baseline instead of having to wait for its first
    # rebuild to acquire one.
    rels = [a.path for n in nodes for a in n.anchors]
    base = resolve_source_base(target.resolve().parent, meta.root, rels, source_root)

    build = writer.build(target, meta, nodes, dangling="drop", source_root=base)
    report.nodes = build.nodes
    report.edges = build.edges
    report.anchors = build.anchors
    report.hashed = build.hashed
    report.dropped_edges = [{"src": s, "dst": d} for s, d in build.dropped_edges]
    return report


def _render(report: MigrationReport) -> str:
    r = report
    lines = [
        f"[codebase-kg] migrated {r.source}",
        f"[codebase-kg]        -> {r.target}",
        "",
        f"  nodes    {r.nodes}   (parsed {r.source_nodes} from markdown)",
        f"  edges    {r.edges}",
        f"  anchors  {r.anchors}",
        "",
        (
            f"  descriptions scrubbed  {r.descriptions_scrubbed}"
            f"  ({r.chars_removed:,} chars of ticket refs / dates / change narrative removed)"
        ),
    ]
    if r.dropped_edges:
        lines.append(f"  dangling edges dropped {len(r.dropped_edges)}")
        for e in r.dropped_edges[:10]:
            lines.append(f"      {e['src']} -> {e['dst']}  (no such node)")
        if len(r.dropped_edges) > 10:
            lines.append(f"      … and {len(r.dropped_edges) - 10} more")
    if r.normalized:
        lines.append(f"  fields normalized      {len(r.normalized)}")
        for n in r.normalized[:10]:
            lines.append(f"      {n['node']}: {n['issue']}")
        if len(r.normalized) > 10:
            lines.append(f"      … and {len(r.normalized) - 10} more")
    if r.needs_rewrite:
        lines += [
            "",
            f"  {len(r.needs_rewrite)} description(s) could not be cleaned automatically and were",
            "  left empty. Run /codebase-kg:refresh to rewrite them from source:",
        ]
        for n in r.needs_rewrite[:20]:
            lines.append(f"      {n['node']}: {n['problem']}")
    lines += [
        "",
        "  The markdown file was not modified. Commit the .db, verify with",
        "  `kg_cli.py query kg_stats` and `kg_cli.py query kg_validate`, then",
        "  delete the markdown when you are happy.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(
        prog="python -m codebase_kg.migrate",
        description="Convert a KNOWLEDGE_GRAPH.md into a committed code_graph.db.",
    )
    ap.add_argument("source", help="path to the existing KNOWLEDGE_GRAPH.md")
    ap.add_argument(
        "-o", "--out", help="output path (default: code_graph.db beside the source)"
    )
    ap.add_argument("--generated", help="override the generated date (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="write to a temp file and report, leaving nothing behind",
    )
    args = ap.parse_args(argv)

    source = Path(args.source).resolve()
    if not source.is_file():
        sys.stderr.write(f"[codebase-kg] no such file: {source}\n")
        return 1

    target = Path(args.out).resolve() if args.out else source.parent / "code_graph.db"
    tmp_target = target
    if args.dry_run:
        import tempfile

        tmp_target = Path(tempfile.mkdtemp()) / "code_graph.db"

    try:
        report = convert(source, tmp_target, generated=args.generated)
    except (writer.BuildError, OSError) as exc:
        sys.stderr.write(f"[codebase-kg] migration failed: {exc}\n")
        return 1
    report.target = str(target)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_render(report))
    if args.dry_run:
        tmp_target.unlink(missing_ok=True)
        print("\n  (--dry-run: nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

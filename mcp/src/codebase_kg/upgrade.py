"""In-place schema upgrade for an existing `code_graph.db`. Stdlib only.

    python -m codebase_kg.upgrade                             # knowledge/code_graph.db
    python -m codebase_kg.upgrade path/to.db --source-root .
    python -m codebase_kg.upgrade --covers 'app/src/**/*.kt' --covers '**/*.gradle.kts'

A v2 graph carries no declared coverage and no anchor hashes, so it cannot
answer either of the questions v3 added — "which files was this supposed to
cover?" and "has the source moved under this description?". This rebuilds it as
v3, preserving every node, anchor and edge verbatim, and stamping hashes from
the working tree if it can find one.

It reads the old file with plain sqlite3 rather than through `CodeGraph`. The
store refuses a schema version it does not speak, and that check is worth
keeping strict — an upgrader is exactly the one caller that legitimately needs
to read an older shape, so it does so explicitly instead of loosening the rule
for everyone.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import cli, links, writer
from .coverage import COVERS_KEY, EXEMPT_KEY, parse_patterns, resolve_source_base
from .links import ExternalLink
from .models import Anchor, Meta, Node
from .schema import SCHEMA_VERSION

DEFAULT_TARGET = Path("knowledge") / "code_graph.db"


class UpgradeError(RuntimeError):
    """The file cannot be upgraded. Nothing was written."""


def read_any_version(path: Path) -> tuple[int, Meta, list[Node], dict[str, str]]:
    """`(version, meta, nodes, sources)` from a graph of any schema written so far.

    Table presence is probed rather than assumed, so a v2 file (which has no
    `source` table) and a v3 one both read through the same path.
    """
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise UpgradeError(f"cannot open {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.DatabaseError as exc:
            raise UpgradeError(f"{path} is not a code graph database ({exc})") from exc
        version = int(rows.get("schema_version", 0) or 0)

        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        sources: dict[str, str] = {}
        if "source" in tables:
            sources = {r["path"]: r["sha"] for r in conn.execute("SELECT path, sha FROM source")}

        anchors: dict[str, list[Anchor]] = {}
        for r in conn.execute("SELECT node_id, path, symbol FROM anchor ORDER BY node_id, ord"):
            anchors.setdefault(r["node_id"], []).append(
                Anchor(path=r["path"], symbol=r["symbol"])
            )
        edges: dict[str, list[str]] = {}
        for r in conn.execute("SELECT src, dst FROM edge ORDER BY src, dst"):
            edges.setdefault(r["src"], []).append(r["dst"])

        # Probed like `source` above: a graph written before `external_link`
        # existed has no such table, and an upgrade that dropped these rows
        # would silently sever every cross-graph link in the file.
        outbound: dict[str, list[ExternalLink]] = {}
        if links.TABLE in tables:
            for node_id, link in links.all_links(conn):
                outbound.setdefault(node_id, []).append(link)

        nodes = [
            Node(
                id=r["id"],
                kind=r["kind"],
                description=r["description"],
                anchors=anchors.get(r["id"], []),
                edges=edges.get(r["id"], []),
                section=r["section"],
                parity=r["parity"],
                counterpart=r["counterpart"],
                divergence=r["divergence"],
                links=outbound.get(r["id"], []),
            )
            for r in conn.execute("SELECT * FROM node ORDER BY id")
        ]
    finally:
        conn.close()

    known = {
        "schema_version", "codebase", "root", "counterpart", "language",
        "generated", COVERS_KEY, EXEMPT_KEY, links.NODE_TABLE_KEY,
    }
    meta = Meta(
        codebase=rows.get("codebase", ""),
        root=rows.get("root", ""),
        counterpart=rows.get("counterpart"),
        language=rows.get("language"),
        generated=rows.get("generated", ""),
        covers=parse_patterns(rows.get(COVERS_KEY)),
        exempt=parse_patterns(rows.get(EXEMPT_KEY)),
        extra={k: v for k, v in rows.items() if k not in known},
    )
    return version, meta, nodes, sources


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(
        prog="python -m codebase_kg.upgrade",
        description=f"Upgrade a code_graph.db to schema v{SCHEMA_VERSION}.",
    )
    ap.add_argument("target", nargs="?", help=f"graph to upgrade (default: {DEFAULT_TARGET})")
    ap.add_argument(
        "--covers",
        action="append",
        default=[],
        help="glob of files the graph should cover, relative to `root` (repeatable). "
        "Without this the upgraded graph still cannot report coverage gaps.",
    )
    ap.add_argument(
        "--exempt",
        action="append",
        default=[],
        help="glob of files deliberately left uncovered (repeatable)",
    )
    ap.add_argument("--source-root", help="repo root the anchor paths resolve from")
    ap.add_argument(
        "--force", action="store_true", help="rebuild even if already at the current version"
    )
    args = ap.parse_args(argv)

    target = Path(args.target) if args.target else DEFAULT_TARGET
    if not target.is_file():
        sys.stderr.write(f"[codebase-kg] no graph at {target}\n")
        return 1

    try:
        version, meta, nodes, sources = read_any_version(target)
    except UpgradeError as exc:
        sys.stderr.write(f"[codebase-kg] {exc}\n")
        return 1

    if version == SCHEMA_VERSION and not (args.covers or args.exempt or args.force):
        print(f"[codebase-kg] {target} is already schema v{version}; nothing to do.")
        return 0

    if args.covers:
        meta.covers = args.covers
    if args.exempt:
        meta.exempt = args.exempt

    rels = [a.path for n in nodes for a in n.anchors]
    base = resolve_source_base(target.resolve().parent, meta.root, rels, args.source_root)

    try:
        report = writer.build(target, meta, nodes, source_root=base, sources=sources)
    except writer.BuildError as exc:
        sys.stderr.write(
            f"[codebase-kg] refused to upgrade: {exc}\n"
            f"[codebase-kg] Nothing was written; {target} is untouched.\n"
        )
        return 1

    print(f"[codebase-kg] upgraded {target}: schema v{version} -> v{SCHEMA_VERSION}")
    print(f"  nodes {report.nodes}   edges {report.edges}   anchors {report.anchors}")
    if base is None:
        print("  no source tree found — anchors carry no baseline hash.")
    else:
        print(f"  hashed {report.hashed}/{report.anchors} anchor(s) against {base}")
    if meta.covers:
        print(f"  covers  {len(meta.covers)} pattern(s)")
        if meta.exempt:
            print(f"  exempt  {len(meta.exempt)} pattern(s)")
    else:
        print("  no `covers` declared — coverage gaps stay invisible. Pass --covers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

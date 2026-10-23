"""`delete_node` and the impact preview it shows first."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import links
from .edit_core import Change, EditError, apply
from .edit_nodes import prune_sources
from .schema import REFERENCE_TABLE, has_reference_table
from .store import CodeGraph


def _impact(conn: sqlite3.Connection, ids: list[str]) -> dict[str, Any]:
    """Everything a delete would take with it, before it takes it.

    `ON DELETE CASCADE` on `anchor`, `edge.src` and `external_link` is deliberate
    -- those rows are parts of the node, not neighbours of it. `edge.dst` is
    RESTRICT for the opposite reason: something else points here, and removing
    that silently is how a graph loses a relationship nobody decided to drop
    (SCHEMA.md §6.2).
    """
    marks = ",".join("?" * len(ids))
    args = tuple(ids)
    missing = sorted(set(ids) - {
        r["id"] for r in conn.execute(f"SELECT id FROM node WHERE id IN ({marks})", args)
    })
    anchors = [
        f"{r['node_id']}: {r['path']}" + (f"#{r['symbol']}" if r["symbol"] else "")
        for r in conn.execute(
            f"SELECT node_id, path, symbol FROM anchor WHERE node_id IN ({marks})"
            " ORDER BY node_id, ord",
            args,
        )
    ]
    outbound = [
        f"{r['src']} -> {r['dst']}"
        for r in conn.execute(
            f"SELECT src, dst FROM edge WHERE src IN ({marks}) ORDER BY src, dst", args
        )
    ]
    inbound = [
        f"{r['src']} -> {r['dst']}"
        for r in conn.execute(
            f"SELECT src, dst FROM edge WHERE dst IN ({marks}) AND src NOT IN ({marks})"
            " ORDER BY dst, src",
            args + args,
        )
    ]
    outgoing_links: list[str] = []
    if links.has_link_table(conn):
        outgoing_links = [
            f"{r['node_id']} -> {r['target']}"
            for r in conn.execute(
                f"SELECT node_id, target FROM {links.TABLE} WHERE node_id IN ({marks})"
                " ORDER BY node_id, target",
                args,
            )
        ]
    cited: list[str] = []
    if has_reference_table(conn):
        cited = [
            f"{r['node_id']} -> {r['url']}"
            for r in conn.execute(
                f"SELECT node_id, url FROM {REFERENCE_TABLE} WHERE node_id IN ({marks})"
                " ORDER BY node_id, ord",
                args,
            )
        ]
    baselines = [
        r["path"]
        for r in conn.execute(
            "SELECT path FROM source WHERE path NOT IN"
            f" (SELECT path FROM anchor WHERE node_id NOT IN ({marks})) ORDER BY path",
            args,
        )
    ]
    counterparts = [
        f"{r['id']} -> {r['counterpart']}"
        for r in conn.execute(
            f"SELECT id, counterpart FROM node WHERE id IN ({marks})"
            " AND counterpart IS NOT NULL ORDER BY id",
            args,
        )
    ]
    return {
        "nodes": sorted(set(ids) - set(missing)),
        "missing": missing,
        "anchors": anchors,
        "outbound_edges": outbound,
        "inbound_edges": inbound,
        "external_links": outgoing_links,
        "references": cited,
        "baselines": baselines,
        "counterparts": counterparts,
    }


def _preview(path: Path, ids: list[str]) -> dict[str, Any]:
    """The same impact calculation, against the committed file, read-only.

    A preview must never copy the graph: the point of `dry_run` is that nothing
    can go wrong, and a code path that writes a temp file to tell you what a
    delete would do has already done more than it promised.
    """
    graph = CodeGraph(path)  # refuses a file that is not a graph this server speaks
    graph.close()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _impact(conn, ids)
    finally:
        conn.close()


def _impact_notes(impact: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    cascading = ("anchors", "outbound_edges", "external_links", "references", "baselines")
    if any(impact[k] for k in cascading):
        notes.append(
            f"cascades: {len(impact['anchors'])} anchor(s), "
            f"{len(impact['outbound_edges'])} outbound edge(s), "
            f"{len(impact['external_links'])} external link(s), "
            f"{len(impact['references'])} reference(s), "
            f"{len(impact['baselines'])} file baseline(s) go with the node(s)"
        )
    if impact["inbound_edges"]:
        notes.append(
            f"{len(impact['inbound_edges'])} edge(s) point AT these nodes and are "
            f"protected by ON DELETE RESTRICT -- pass cascade_inbound to remove them, "
            f"or re-point them first"
        )
    if impact["counterparts"]:
        notes.append(
            "these nodes carry a counterpart, so the peer graph's back-link will "
            "dangle until it is edited too (parity is reciprocal, SCHEMA.md §9)"
        )
    return notes


def delete_node(
    path: str | Path,
    ids: Iterable[str],
    *,
    dry_run: bool = True,
    cascade_inbound: bool = False,
) -> dict[str, Any]:
    """Delete nodes, after saying what goes with them.

    `dry_run` defaults to true because the blast radius is not obvious from the
    call: a node takes its anchors, its outbound edges and its external links
    with it, and any of those may be the thing the caller actually cared about.
    """
    wanted = sorted({str(i).strip() for i in ids if str(i).strip()})
    if not wanted:
        raise EditError("no node ids given")

    if dry_run:
        impact = _preview(Path(path), wanted)
        return {
            "ok": True,
            "written": False,
            "dry_run": True,
            "path": Path(path).as_posix(),
            "would_delete": impact,
            "notes": _impact_notes(impact),
            "next": "call again with dry_run=false to apply",
        }

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        impact = _impact(conn, wanted)
        if impact["missing"]:
            raise EditError(
                f"no such node(s): {', '.join(impact['missing'])}. Nothing was deleted."
            )
        changes: list[Change] = []
        if impact["inbound_edges"] and not cascade_inbound:
            raise EditError(
                f"{len(impact['inbound_edges'])} edge(s) point at these nodes and "
                f"ON DELETE RESTRICT blocks the delete: "
                f"{', '.join(impact['inbound_edges'][:10])}. Re-point them, or pass "
                f"cascade_inbound=true to delete them along with the node(s)."
            )
        marks = ",".join("?" * len(wanted))
        if impact["inbound_edges"]:
            conn.execute(f"DELETE FROM edge WHERE dst IN ({marks})", tuple(wanted))
            changes.append(
                Change("edge", ", ".join(wanted), "deleted",
                       {"inbound_edges": (impact["inbound_edges"], [])})
            )
        # Edges *between* two doomed nodes go first. They cascade from the src
        # side, but RESTRICT fires from the dst side the moment the first of the
        # pair is deleted -- so deleting two mutually-linked nodes would fail on a
        # constraint protecting a node that is itself on the way out.
        conn.execute(
            f"DELETE FROM edge WHERE src IN ({marks}) AND dst IN ({marks})",
            tuple(wanted) * 2,
        )
        for node_id in wanted:
            # FTS is a virtual table with no foreign key, so it does not cascade.
            # Left behind, the row keeps answering `kg_search` for a node that
            # no longer exists -- a hit that leads nowhere.
            conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM node WHERE id = ?", (node_id,))
        changes.append(
            Change("node", ", ".join(wanted), "deleted",
                   {
                       "nodes": (impact["nodes"], []),
                       "anchors": (impact["anchors"], []),
                       "outbound_edges": (impact["outbound_edges"], []),
                       "external_links": (impact["external_links"], []),
                       "references": (impact["references"], []),
                   })
        )
        changes += prune_sources(conn)
        return changes

    out = apply(path, mutate)
    out["dry_run"] = False
    return out

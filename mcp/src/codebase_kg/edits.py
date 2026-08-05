"""Targeted writes to a committed `code_graph.db`. Stdlib only.

`export -> edit the JSON -> build` stays. It is the right shape for bulk work -- a
parity sweep over every node, a restructuring that touches half the edges,
anything where reviewing the diff before building it is the point. What it is
not is a reasonable way to fix one node's description: regenerating the whole
artifact to change one string costs a full read/write cycle and leaves a scratch
JSON file in the repo root that somebody has to remember to delete.

This module is the other half. Three properties make a write safe enough to
point at a committed artifact:

**It lands whole or not at all.** Every operation runs against a private copy of
the file, inside one SQLite transaction, and the copy is moved over the original
only once everything has passed. A CHECK violated by the third of five nodes
leaves the committed graph *untouched* rather than rolled back -- nothing ever
opened it for writing. This is the mechanism `writer.build` already uses, and
reusing it keeps `CodeGraph` strictly read-only: the artifact is opened `mode=ro`
by every reader in the package, and a bug in an edit still cannot mutate it.

**It is validated, not merely constrained.** The CHECKs reject a malformed row.
They cannot see that deleting a node stranded the counterpart the peer graph
links back to, because that fact lives in another file. So `kg_validate` -- the
same function the tool calls, not a reimplementation -- runs against the copy and
the write is refused if it introduced a finding the graph did not already have.
Not "if the graph is clean": a real graph usually carries findings, and demanding
zero would make writes impossible on exactly the graphs that need editing.

**It says what it did.** The round trip gave review for free -- you read the JSON
diff before building it. A tool that mutates silently takes that away, so every
operation returns the rows it touched, field by field, before and after.

Nothing here holds the graph open across a call. Windows refuses to replace a
file anyone has open, which is why the server does not cache a handle; a write
that kept one would make the next `/codebase-kg:refresh` fail.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clean, links, tools, writer
from .links import ExternalLink, LinkError
from .models import Anchor, Node
from .store import CodeGraph, StoreError

#: Scalar node columns an upsert may set, in the order they appear in the DDL.
#: `id` is not here: it is the key, not a field, and "renaming" a node is a
#: delete plus an insert with different edge consequences.
SCALAR_FIELDS = ("kind", "description", "section", "parity", "counterpart", "divergence")

#: Fields whose value is a list, replaced wholesale when present.
LIST_FIELDS = ("anchors", "edges", "external_links")


class EditError(ValueError):
    """The edit was refused. The committed graph is byte-identical to before."""


@dataclass
class Change:
    """One row the edit moved, and which of its fields moved.

    Named per table rather than per node so a reviewer can see that deleting one
    node also removed six edges -- the thing a silent cascade hides.
    """

    table: str
    key: str
    action: str  # created | updated | deleted
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "key": self.key,
            "action": self.action,
            "fields": [
                {"field": name, "before": before, "after": after}
                for name, (before, after) in sorted(self.fields.items())
            ],
        }


#: A mutation runs inside the transaction and returns what it changed. Returning
#: nothing means "nothing moved", and the copy is discarded rather than swapped
#: in -- so a no-op edit leaves the file's bytes and its git diff alone.
Mutate = Callable[[sqlite3.Connection], "list[Change]"]


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def _validation(path: Path) -> dict[str, Any]:
    """Run the graph through `kg_validate`, peer and all.

    The peer matters: parity is reciprocal (SCHEMA.md §9), so a write that breaks
    a back-link is only visible with the counterpart graph open. Validating
    without it would accept the one class of damage the check exists for.
    """
    graph = CodeGraph(path)
    try:
        with tools.open_peer(graph) as peer:
            return tools.kg_validate(graph, peer)
    finally:
        graph.close()


def _blocking(report: Mapping[str, Any]) -> set[str]:
    """The findings that make `kg_validate`'s `ok` false, as comparable strings.

    Only these can refuse a write. `changed_since_built` and an absent peer graph
    are advisory by design (SCHEMA.md §6.3, GRAPH-LINKS.md §4) -- blocking on them
    would reject an edit for a fact about the source tree that the edit neither
    caused nor can fix, which is how a check trains people to bypass it.
    """
    out = {f"counterpart {i['node']}: {i['issue']}" for i in report["counterpart_issues"]}
    out |= {f"description {i['node']}: {i['issue']}" for i in report["description_issues"]}
    out |= {f"anchor {i['node']} {i['anchor']}: {i['issue']}" for i in report["anchor_issues"]}
    out |= {
        f"external-link {i['node']} -> {i['target']}: {i['issue']}"
        for i in report["external_link_issues"]
        if i["severity"] == "error"
    }
    out |= {f"coverage gap: {p}" for p in report["coverage"].get("gaps", [])}
    return out


def _advisory(report: Mapping[str, Any]) -> set[str]:
    """Findings worth reporting but never worth refusing a write over."""
    return {
        f"external-link {i['node']} -> {i['target']}: {i['issue']}"
        for i in report["external_link_issues"]
        if i["severity"] != "error"
    }


def _new_findings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """What this edit broke that was not already broken.

    The test is "no NEW findings", never "clean". A graph mid-refactor legitimately
    has open findings, and a write tool that refused to touch it would be useless
    precisely when it is needed.
    """
    new = sorted(_blocking(after) - _blocking(before))
    # `coverage.gaps` is capped at 50 entries, so on a graph with a long tail a
    # newly uncovered file may never appear in the list. Comparing the counts as
    # well is what stops a delete from quietly uncovering a file past the cap.
    was = int(before["coverage"].get("gap_count", 0))
    now = int(after["coverage"].get("gap_count", 0))
    if now > was and not any(f.startswith("coverage gap:") for f in new):
        new.append(f"coverage gap count rose from {was} to {now}")
    return new


def _explain(exc: sqlite3.IntegrityError) -> str:
    """A constraint failure in terms of the contract it enforces.

    Most violations are caught before the write by `clean.node_problems`, which
    names the node. This is the backstop for the ones only SQL can see, and it
    exists so an agent never receives a bare `IntegrityError` naming a table.
    """
    text = str(exc)
    if "FOREIGN KEY" in text:
        return (
            f"{text} -- an edge or anchor points at a node that does not exist, or a "
            f"delete was blocked by another node's edge into it (SCHEMA.md §6.2)"
        )
    if "UNIQUE" in text or "PRIMARY KEY" in text:
        return f"{text} -- that row is already in the graph"
    return text


def apply(path: str | Path, mutate: Mutate) -> dict[str, Any]:
    """Run one mutation against a copy of the graph, validate it, then swap it in.

    Raises `EditError` -- having written nothing -- when the mutation violates a
    constraint or introduces a validation finding the graph did not already have.
    """
    target = Path(path)
    if not target.is_file():
        raise EditError(
            f"no code graph at {target}. Create one with /codebase-kg:build before editing."
        )

    before = _validation(target)

    # Same directory as the target, because `os.replace` is only atomic within a
    # filesystem -- and because `kg_validate` resolves anchor paths and the peer
    # graph relative to the graph's own directory, so a copy elsewhere would
    # validate against a different world than the one it is about to become.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".edit"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(target, tmp)
        conn = sqlite3.connect(str(tmp))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                changes = mutate(conn)
        except sqlite3.IntegrityError as exc:
            raise EditError(_explain(exc)) from exc
        except sqlite3.Error as exc:
            raise EditError(f"the write failed: {exc}") from exc
        finally:
            conn.close()

        if not changes:
            tmp.unlink(missing_ok=True)
            return {
                "ok": True,
                "written": False,
                "path": target.as_posix(),
                "changes": [],
                "note": "every field already held that value; the graph was not rewritten",
            }

        after = _validation(tmp)
        new = _new_findings(before, after)
        if new:
            raise EditError(
                "the edit is valid SQL but breaks the graph, so it was rolled back. "
                "New findings: " + "; ".join(new)
            )
        try:
            writer.replace_file(tmp, target)
        except writer.BuildError as exc:
            raise EditError(str(exc)) from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    return {
        "ok": True,
        "written": True,
        "path": target.as_posix(),
        "changes": [c.to_dict() for c in changes],
        "validation": {
            "ok_before": bool(before["ok"]),
            "ok_after": bool(after["ok"]),
            "new_findings": [],
            "new_warnings": sorted(_advisory(after) - _advisory(before)),
        },
    }


# --------------------------------------------------------------------------- #
# Reading the current row, on the edit's own connection
# --------------------------------------------------------------------------- #
def _read_node(conn: sqlite3.Connection, node_id: str) -> Node | None:
    """The node as it stands, hydrated from the connection being edited.

    Not through `CodeGraph`: that opens its own read-only handle on the file, and
    would therefore not see rows this transaction has already written -- so a call
    upserting two nodes could not have the second one see the first.
    """
    row = conn.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    anchors = [
        Anchor(path=r["path"], symbol=r["symbol"])
        for r in conn.execute(
            "SELECT path, symbol FROM anchor WHERE node_id = ? ORDER BY ord", (node_id,)
        )
    ]
    edges = [
        r["dst"]
        for r in conn.execute("SELECT dst FROM edge WHERE src = ? ORDER BY dst", (node_id,))
    ]
    outbound = links.links_for(conn, node_id) if links.has_link_table(conn) else []
    return Node(
        id=row["id"],
        kind=row["kind"],
        description=row["description"],
        anchors=anchors,
        edges=edges,
        section=row["section"],
        parity=row["parity"],
        counterpart=row["counterpart"],
        divergence=row["divergence"],
        links=outbound,
    )


def _ensure_link_table(conn: sqlite3.Connection) -> list[Change]:
    """Create `external_link` if this graph predates it.

    The table was added without a schema bump precisely so an older committed
    artifact still opens (schema.py). The flip side is that the first link
    written to such a graph has to create it -- otherwise adopting the mechanism
    would require a rebuild, which is what "additive" is supposed to avoid.
    """
    if links.has_link_table(conn):
        return []
    conn.executescript(links.EXTERNAL_LINK_DDL)
    return [Change("external_link", links.TABLE, "created")]


# --------------------------------------------------------------------------- #
# kg_upsert_node
# --------------------------------------------------------------------------- #
def _parse_anchors(raw: object, node_id: str) -> list[Anchor]:
    if not isinstance(raw, (list, tuple)):
        raise EditError(f"node '{node_id}': `anchors` must be a list of 'path#Symbol' strings")
    out: list[Anchor] = []
    for item in raw:
        if isinstance(item, str):
            out.append(Anchor.parse(item))
        elif isinstance(item, Mapping):
            path = str(item.get("path", "")).strip()
            symbol = item.get("symbol")
            out.append(Anchor(path=path, symbol=str(symbol) if symbol else None))
        else:
            raise EditError(f"node '{node_id}': anchor {item!r} must be a string or an object")
    return out


def _parse_links(raw: object, node_id: str) -> list[ExternalLink]:
    if not isinstance(raw, (list, tuple)):
        raise EditError(f"node '{node_id}': `external_links` must be a list")
    try:
        return links.dedupe(ExternalLink.parse(item) for item in raw)
    except LinkError as exc:
        raise EditError(f"node '{node_id}': {exc}") from exc


def _merge(current: Node | None, patch: Mapping[str, Any]) -> Node:
    """The node as it will be: `current` with the supplied keys applied.

    Absent means unchanged; explicit `null` clears a nullable column. The
    distinction matters because most edits touch one field, and requiring the
    whole node back would make every targeted write a chance to drop something
    the caller never looked at.
    """
    node_id = str(patch["id"]).strip()
    if current is None:
        merged = Node(id=node_id)
    else:
        merged = Node(
            id=current.id,
            kind=current.kind,
            description=current.description,
            anchors=list(current.anchors),
            edges=list(current.edges),
            section=current.section,
            parity=current.parity,
            counterpart=current.counterpart,
            divergence=current.divergence,
            links=list(current.links),
        )
    for name in SCALAR_FIELDS:
        if name not in patch:
            continue
        value = patch[name]
        if value is None:
            # `kind`, `description` and `section` are NOT NULL with a default;
            # only the parity triple is genuinely nullable.
            setattr(merged, name, None if name in {"parity", "counterpart", "divergence"} else "")
        else:
            setattr(merged, name, str(value))
    if "anchors" in patch:
        merged.anchors = _parse_anchors(patch["anchors"] or [], node_id)
    if "edges" in patch:
        raw = patch["edges"] or []
        if not isinstance(raw, (list, tuple)):
            raise EditError(f"node '{node_id}': `edges` must be a list of node ids")
        merged.edges = sorted({str(e).strip() for e in raw if str(e).strip()})
    if "external_links" in patch:
        merged.links = _parse_links(patch["external_links"] or [], node_id)
    return merged


def _write_node(conn: sqlite3.Connection, before: Node | None, after: Node) -> list[Change]:
    """One node's row, anchors, links and FTS entry. Edges are written later."""
    changes: list[Change] = []
    scalars = {
        name: (getattr(before, name) if before else None, getattr(after, name))
        for name in SCALAR_FIELDS
    }
    moved = {k: v for k, v in scalars.items() if before is None or v[0] != v[1]}

    if before is None:
        conn.execute(
            "INSERT INTO node (id, kind, description, section, parity, counterpart, divergence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (after.id, after.kind, after.description, after.section,
             after.parity, after.counterpart, after.divergence),
        )
        changes.append(Change("node", after.id, "created", moved))
    elif moved:
        conn.execute(
            "UPDATE node SET kind = ?, description = ?, section = ?, parity = ?,"
            " counterpart = ?, divergence = ? WHERE id = ?",
            (after.kind, after.description, after.section, after.parity,
             after.counterpart, after.divergence, after.id),
        )
        changes.append(Change("node", after.id, "updated", moved))

    old_anchors = [str(a) for a in (before.anchors if before else [])]
    new_anchors = [str(a) for a in after.anchors]
    if old_anchors != new_anchors:
        # Replaced wholesale rather than diffed: `ord` is the author's order and
        # is part of the primary key, so a positional edit would have to shuffle
        # keys around a unique constraint to say "insert one in the middle".
        conn.execute("DELETE FROM anchor WHERE node_id = ?", (after.id,))
        conn.executemany(
            "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, ?, ?, ?, ?)",
            [(after.id, i, a.path, a.base, a.symbol) for i, a in enumerate(after.anchors)],
        )
        changes.append(
            Change("anchor", after.id, "updated", {"anchors": (old_anchors, new_anchors)})
        )

    old_links = [link.as_dict() for link in (before.links if before else [])]
    new_links = [link.as_dict() for link in after.links]
    if old_links != new_links:
        changes += _ensure_link_table(conn)
        conn.execute(f"DELETE FROM {links.TABLE} WHERE node_id = ?", (after.id,))
        for link in after.links:
            links.insert(conn, after.id, link)
        changes.append(
            Change("external_link", after.id, "updated", {"links": (old_links, new_links)})
        )

    # The FTS row is not foreign-keyed to anything -- a virtual table cannot be --
    # so it is the one thing that silently rots if an upsert forgets it: the node
    # would still answer to `kg_node` and stop answering to `kg_search`.
    text = writer.fts_text(after)
    if before is None or writer.fts_text(before) != text:
        conn.execute("DELETE FROM node_fts WHERE node_id = ?", (after.id,))
        conn.execute("INSERT INTO node_fts (node_id, text) VALUES (?, ?)", (after.id, text))
    return changes


def _write_edges(conn: sqlite3.Connection, before: Node | None, after: Node) -> list[Change]:
    old = sorted(before.edges) if before else []
    new = sorted(after.edges)
    if old == new:
        return []
    conn.execute("DELETE FROM edge WHERE src = ?", (after.id,))
    conn.executemany(
        "INSERT INTO edge (src, dst) VALUES (?, ?)", [(after.id, dst) for dst in new]
    )
    return [Change("edge", after.id, "updated", {"edges": (old, new)})]


def upsert_node(path: str | Path, nodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Create or update nodes. Only the keys supplied are touched.

    Raises `EditError`, having written nothing, if any node in the batch is
    rejected -- including the fifth of five.
    """
    patches = [dict(n) for n in nodes]
    if not patches:
        raise EditError("no nodes given")
    for patch in patches:
        if not str(patch.get("id", "")).strip():
            raise EditError("every node needs an `id`")
    ids = [str(p["id"]).strip() for p in patches]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise EditError(
            f"the same node appears twice in one call: {', '.join(duplicated)}. "
            f"Merge them -- the second would silently win."
        )

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        changes: list[Change] = []
        staged: list[tuple[Node | None, Node]] = []
        for patch in patches:
            node_id = str(patch["id"]).strip()
            current = _read_node(conn, node_id)
            if current is None and not str(patch.get("kind", "")).strip():
                raise EditError(
                    f"node '{node_id}' does not exist yet, so it needs a `kind` "
                    f"(SCHEMA.md §4). Free text -- 'ViewModel', 'Service', 'module'."
                )
            merged = _merge(current, patch)
            problems = clean.node_problems(merged)
            if problems:
                raise EditError(f"node '{merged.id}': {'; '.join(problems)}")
            staged.append((current, merged))

        # Every node row first, then every edge. A batch that introduces two
        # nodes pointing at each other is a normal thing to write, and it is only
        # possible if no edge is inserted before both endpoints exist.
        for current, merged in staged:
            changes += _write_node(conn, current, merged)
        for current, merged in staged:
            for dst in merged.edges:
                if dst == merged.id:
                    raise EditError(f"node '{merged.id}' cannot have an edge to itself")
            changes += _write_edges(conn, current, merged)
        return changes

    return apply(path, mutate)


# --------------------------------------------------------------------------- #
# kg_delete_node
# --------------------------------------------------------------------------- #
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
    if impact["outbound_edges"] or impact["anchors"] or impact["external_links"]:
        notes.append(
            f"cascades: {len(impact['anchors'])} anchor(s), "
            f"{len(impact['outbound_edges'])} outbound edge(s), "
            f"{len(impact['external_links'])} external link(s) go with the node(s)"
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
                   })
        )
        return changes

    out = apply(path, mutate)
    out["dry_run"] = False
    return out


# --------------------------------------------------------------------------- #
# kg_add_link / kg_remove_link
# --------------------------------------------------------------------------- #
def add_link(path: str | Path, node_id: str, target: str, kind: str = "") -> dict[str, Any]:
    """Point a code node at a node in another committed graph in this repo."""
    node_id = str(node_id).strip()
    try:
        link = ExternalLink(target=str(target).strip(), kind=str(kind or "").strip())
        links.split_target(link.target)
    except LinkError as exc:
        raise EditError(str(exc)) from exc

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        if _read_node(conn, node_id) is None:
            raise EditError(f"no node '{node_id}' in this graph")
        changes = _ensure_link_table(conn)
        row = conn.execute(
            f"SELECT kind FROM {links.TABLE} WHERE node_id = ? AND target = ?",
            (node_id, link.target),
        ).fetchone()
        if row is not None:
            if row["kind"] == link.kind:
                return []
            # `(node_id, target)` is the primary key, so one node cannot call one
            # target both `implements` and `tests` -- that is the constraint, not
            # an accident, so changing the kind is an explicit two-step.
            raise EditError(
                f"'{node_id}' already links to {link.target!r} as {row['kind']!r}. "
                f"Remove that link first if it should be {link.kind!r} instead."
            )
        links.insert(conn, node_id, link)
        changes.append(
            Change("external_link", f"{node_id} -> {link.target}", "created",
                   {"target": (None, link.target), "kind": (None, link.kind)})
        )
        return changes

    return apply(path, mutate)


def remove_link(path: str | Path, node_id: str, target: str) -> dict[str, Any]:
    """Drop one cross-graph pointer. The node and everything else stay."""
    node_id = str(node_id).strip()
    wanted = links.normalize_target(str(target))

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        if not links.has_link_table(conn):
            raise EditError("this graph has no external links")
        row = conn.execute(
            f"SELECT kind FROM {links.TABLE} WHERE node_id = ? AND target = ?",
            (node_id, wanted),
        ).fetchone()
        if row is None:
            raise EditError(f"'{node_id}' does not link to {wanted!r}")
        conn.execute(
            f"DELETE FROM {links.TABLE} WHERE node_id = ? AND target = ?", (node_id, wanted)
        )
        return [
            Change("external_link", f"{node_id} -> {wanted}", "deleted",
                   {"target": (wanted, None), "kind": (row["kind"], None)})
        ]

    return apply(path, mutate)


__all__ = [
    "EditError",
    "Change",
    "apply",
    "upsert_node",
    "delete_node",
    "add_link",
    "remove_link",
    "StoreError",
]

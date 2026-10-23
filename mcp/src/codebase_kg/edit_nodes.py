"""Node reads, patch parsing and `upsert_node`."""

from __future__ import annotations

import difflib
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import clean, cli, links, writer
from .coverage import resolve_source_base
from .edit_core import Change, EditError, apply
from .links import ExternalLink, LinkError
from .models import Anchor, Node, Reference, ReferenceFormatError
from .schema import REFERENCE_DDL, REFERENCE_TABLE, SCHEMA_VERSION, has_reference_table

#: Scalar node columns an upsert may set, in the order they appear in the DDL.
#: `id` is not here: it is the key, not a field, and "renaming" a node is a
#: delete plus an insert with different edge consequences.
SCALAR_FIELDS = ("kind", "description", "section", "parity", "counterpart", "divergence")

NODE_KEYS = (
    "id", *SCALAR_FIELDS, "anchors", "edges", "external_links", "references", "rebaseline"
)
ANCHOR_KEYS = ("path", "symbol")
LINK_KEYS = ("target", "kind")
REFERENCE_KEYS = ("url", "kind", "title", "path", "symbol")


def read_node(conn: sqlite3.Connection, node_id: str) -> Node | None:
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
    cited: list[Reference] = []
    if has_reference_table(conn):
        cited = [
            Reference.from_row(r)
            for r in conn.execute(
                f"SELECT kind, title, url, path, symbol FROM {REFERENCE_TABLE}"
                " WHERE node_id = ? ORDER BY ord",
                (node_id,),
            )
        ]
    return Node.from_row(row, anchors=anchors, edges=edges, links=outbound, references=cited)


def _ensure_reference_table(conn: sqlite3.Connection) -> list[Change]:
    """Create `reference` in a v3 graph, and move its version stamp with it.

    The stamp moves because a v3-era server cannot see the table: its next
    export -> build would write the graph back without these rows and call that
    a success. At v4 it refuses the file and names the fix instead (schema.py).
    """
    if has_reference_table(conn):
        return []
    conn.execute(REFERENCE_DDL)
    changes = [Change("reference", REFERENCE_TABLE, "created")]
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    found = int(row["value"]) if row else 0
    if found < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
        )
        changes.append(
            Change("meta", "schema_version", "updated",
                   {"schema_version": (str(found), str(SCHEMA_VERSION))})
        )
    return changes


def ensure_link_table(conn: sqlite3.Connection) -> list[Change]:
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


def prune_sources(conn: sqlite3.Connection) -> list[Change]:
    orphans = conn.execute(
        "SELECT path, sha FROM source WHERE path NOT IN (SELECT path FROM anchor)"
        " ORDER BY path"
    ).fetchall()
    conn.executemany("DELETE FROM source WHERE path = ?", [(r["path"],) for r in orphans])
    return [Change("source", r["path"], "deleted", {"sha": (r["sha"], None)}) for r in orphans]


def _stamp_sources(
    conn: sqlite3.Connection, graph_dir: Path, nodes: list[Node], rehash: set[str]
) -> list[Change]:
    """Hash through `writer.stamp_hashes`, never directly: an upsert and a build
    must record the same digest for the same file, or the hooks read it as drift."""
    recorded = {r["path"]: r["sha"] for r in conn.execute("SELECT path, sha FROM source")}
    # Never overwrite an existing baseline without `rebaseline`: that re-blesses
    # drift nobody checked.
    kept = {p: s for p, s in recorded.items() if p not in rehash}
    wanted = {a.path for n in nodes for a in n.anchors}
    if wanted <= kept.keys():
        return []

    row = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
    anchored = [
        r["path"] for r in conn.execute("SELECT DISTINCT path FROM anchor ORDER BY path")
    ]
    base = resolve_source_base(graph_dir, row["value"] if row else "", anchored)
    report = writer.BuildReport()
    fresh = writer.stamp_hashes(nodes, base, report, kept) if base is not None else kept

    unread = sorted(p for p in rehash if p not in fresh)
    if unread:
        where = f"under {base.as_posix()}" if base is not None else "(no source tree found)"
        raise EditError(
            f"cannot re-baseline {', '.join(unread)}: the file cannot be read {where}. "
            f"A baseline records what the file holds now, so there is nothing to record."
        )

    changes: list[Change] = []
    for path in sorted(wanted & fresh.keys()):
        before, after = recorded.get(path), fresh[path]
        if before == after:
            continue
        conn.execute(
            "INSERT INTO source (path, sha) VALUES (?, ?)"
            " ON CONFLICT (path) DO UPDATE SET sha = excluded.sha",
            (path, after),
        )
        action = "created" if before is None else "updated"
        changes.append(Change("source", path, action, {"sha": (before, after)}))
    return changes


# --------------------------------------------------------------------------- #
# kg_upsert_node
# --------------------------------------------------------------------------- #
def _reject_unknown(raw: Mapping[str, Any], accepted: tuple[str, ...], where: str) -> None:
    """Refuse a key outside `accepted`, naming the closest accepted key.

    A misspelled key that is dropped silently reads as a successful write, so the
    caller never learns the field was not set.
    """
    unknown = [str(key) for key in raw if key not in accepted]
    if not unknown:
        return
    found = []
    for key in unknown:
        close = difflib.get_close_matches(key, accepted, n=1)
        found.append(f"unknown key {key!r}" + (f", did you mean {close[0]!r}?" if close else ""))
    raise EditError(
        f"{where}: {'; '.join(found)} Accepted keys: {', '.join(accepted)}. Nothing was written."
    )


def _parse_anchors(raw: object, node_id: str) -> list[Anchor]:
    if not isinstance(raw, (list, tuple)):
        raise EditError(f"node '{node_id}': `anchors` must be a list of 'path#Symbol' strings")
    out: list[Anchor] = []
    for item in raw:
        if isinstance(item, str):
            out.append(Anchor.parse(item))
        elif isinstance(item, Mapping):
            _reject_unknown(item, ANCHOR_KEYS, f"node '{node_id}', anchor {dict(item)!r}")
            path = str(item.get("path") or "").strip()
            symbol = item.get("symbol")
            out.append(Anchor(path=path, symbol=str(symbol) if symbol else None))
        else:
            raise EditError(f"node '{node_id}': anchor {item!r} must be a string or an object")
    return out


def _parse_links(raw: object, node_id: str) -> list[ExternalLink]:
    if not isinstance(raw, (list, tuple)):
        raise EditError(f"node '{node_id}': `external_links` must be a list")
    for item in raw:
        if isinstance(item, Mapping):
            _reject_unknown(item, LINK_KEYS, f"node '{node_id}', external link {dict(item)!r}")
    try:
        return links.dedupe(ExternalLink.parse(item) for item in raw)
    except LinkError as exc:
        raise EditError(f"node '{node_id}': {exc}") from exc


def _parse_references(raw: object, node_id: str) -> list[Reference]:
    if not isinstance(raw, (list, tuple)):
        raise EditError(f"node '{node_id}': `references` must be a list")
    for item in raw:
        if isinstance(item, Mapping):
            _reject_unknown(item, REFERENCE_KEYS, f"node '{node_id}', reference {dict(item)!r}")
    try:
        return [Reference.parse(item) for item in raw]
    except ReferenceFormatError as exc:
        raise EditError(f"node '{node_id}': {exc}") from exc


def _rebaseline_flag(patch: Mapping[str, Any], node_id: str) -> bool:
    value = patch.get("rebaseline", False)
    if not isinstance(value, bool):
        raise EditError(f"node '{node_id}': `rebaseline` must be true or false, not {value!r}")
    return value


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
            references=list(current.references),
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
    merged.tidy_parity_fields()
    if "anchors" in patch:
        merged.anchors = _parse_anchors(patch["anchors"] or [], node_id)
    if "edges" in patch:
        raw = patch["edges"] or []
        if not isinstance(raw, (list, tuple)):
            raise EditError(f"node '{node_id}': `edges` must be a list of node ids")
        merged.edges = sorted({str(e).strip() for e in raw if str(e).strip()})
    if "external_links" in patch:
        merged.links = _parse_links(patch["external_links"] or [], node_id)
    if "references" in patch:
        merged.references = _parse_references(patch["references"] or [], node_id)
    return merged


def write_node(conn: sqlite3.Connection, before: Node | None, after: Node) -> list[Change]:
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
        changes += ensure_link_table(conn)
        conn.execute(f"DELETE FROM {links.TABLE} WHERE node_id = ?", (after.id,))
        for link in after.links:
            links.insert(conn, after.id, link)
        changes.append(
            Change("external_link", after.id, "updated", {"links": (old_links, new_links)})
        )

    old_refs = [ref.as_dict() for ref in (before.references if before else [])]
    new_refs = [ref.as_dict() for ref in after.references]
    if old_refs != new_refs:
        changes += _ensure_reference_table(conn)
        conn.execute(f"DELETE FROM {REFERENCE_TABLE} WHERE node_id = ?", (after.id,))
        writer.insert_references(conn, [after])
        changes.append(
            Change("reference", after.id, "updated", {"references": (old_refs, new_refs)})
        )

    # The FTS row is not foreign-keyed to anything -- a virtual table cannot be --
    # so it is the one thing that silently rots if an upsert forgets it: the node
    # would still answer to `kg_node` and stop answering to `kg_search`.
    text = writer.fts_text(after)
    if before is None or writer.fts_text(before) != text:
        conn.execute("DELETE FROM node_fts WHERE node_id = ?", (after.id,))
        conn.execute("INSERT INTO node_fts (node_id, text) VALUES (?, ?)", (after.id, text))
    return changes


def _forbids_self_edges(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'edge'"
    ).fetchone()
    return row is not None and "src <> dst" in str(row["sql"])


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
    for patch in patches:
        _reject_unknown(patch, NODE_KEYS, f"node '{str(patch['id']).strip()}'")
    rebaseline = {node_id: _rebaseline_flag(p, node_id) for node_id, p in zip(ids, patches)}
    graph_dir = Path(path).resolve().parent

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        changes: list[Change] = []
        staged: list[tuple[Node | None, Node]] = []
        for patch in patches:
            node_id = str(patch["id"]).strip()
            current = read_node(conn, node_id)
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
            changes += write_node(conn, current, merged)
        for current, merged in staged:
            if merged.id in merged.edges and _forbids_self_edges(conn):
                upgrade = cli.command("upgrade", "--force", f'"{path}"')
                raise EditError(
                    f"node '{merged.id}' has an edge to itself, but this graph predates "
                    f"self-edges and its edge table rejects them. Rebuild it with `{upgrade}`, "
                    "then repeat the edit."
                )
            changes += _write_edges(conn, current, merged)
        written = [merged for _, merged in staged]
        rehash = {a.path for n in written if rebaseline[n.id] for a in n.anchors}
        changes += prune_sources(conn)
        changes += _stamp_sources(conn, graph_dir, written, rehash)
        return changes

    return apply(path, mutate)

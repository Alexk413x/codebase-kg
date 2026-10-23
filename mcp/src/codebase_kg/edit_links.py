"""`add_link`, `remove_link`, `add_reference` and `remove_reference`."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from . import clean, links
from .edit_core import Change, EditError, apply
from .edit_nodes import ensure_link_table, read_node, write_node
from .links import ExternalLink, LinkError
from .models import Node, Reference, ReferenceFormatError


def add_link(path: str | Path, node_id: str, target: str, kind: str = "") -> dict[str, Any]:
    """Point a code node at a node in another committed graph in this repo."""
    node_id = str(node_id).strip()
    try:
        link = ExternalLink(target=str(target).strip(), kind=str(kind or "").strip())
        links.split_target(link.target)
    except LinkError as exc:
        raise EditError(str(exc)) from exc

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        if read_node(conn, node_id) is None:
            raise EditError(f"no node '{node_id}' in this graph")
        changes = ensure_link_table(conn)
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


def _rewrite_references(
    conn: sqlite3.Connection, current: Node, references: list[Reference]
) -> list[Change]:
    merged = dataclasses.replace(current, references=references)
    problems = clean.node_problems(merged)
    if problems:
        raise EditError(f"node '{merged.id}': {'; '.join(problems)}")
    return write_node(conn, current, merged)


def add_reference(
    path: str | Path,
    node_id: str,
    url: str,
    kind: str = "",
    title: str = "",
    ref_path: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Record where a fact this node depends on is documented."""
    node_id = str(node_id).strip()
    try:
        ref = Reference.parse(
            {"url": url, "kind": kind, "title": title, "path": ref_path, "symbol": symbol}
        )
    except ReferenceFormatError as exc:
        raise EditError(str(exc)) from exc

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        current = read_node(conn, node_id)
        if current is None:
            raise EditError(f"no node '{node_id}' in this graph")
        if ref in current.references:
            return []
        return _rewrite_references(conn, current, [*current.references, ref])

    return apply(path, mutate)


def remove_reference(
    path: str | Path,
    node_id: str,
    url: str,
    ref_path: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Drop a node's reference(s) to `url`; `ref_path` / `symbol` pick one narrowing."""
    node_id = str(node_id).strip()
    wanted = Reference(url=url, path=ref_path, symbol=symbol)

    def mutate(conn: sqlite3.Connection) -> list[Change]:
        current = read_node(conn, node_id)
        if current is None:
            raise EditError(f"no node '{node_id}' in this graph")
        narrowed = wanted.path is not None or wanted.symbol is not None
        kept = [
            r for r in current.references
            if r.url != wanted.url
            or (narrowed and (r.path, r.symbol) != (wanted.path, wanted.symbol))
        ]
        if len(kept) == len(current.references):
            where = f" narrowed to '{wanted.narrowing}'" if narrowed else ""
            raise EditError(f"'{node_id}' has no reference to {wanted.url!r}{where}")
        return _rewrite_references(conn, current, kept)

    return apply(path, mutate)

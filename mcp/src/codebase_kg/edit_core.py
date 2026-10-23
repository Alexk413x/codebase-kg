"""The edit engine: change records, validation against a private copy, atomic swap."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tools, writer
from .store import CodeGraph


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


def _validation(path: Path, cache: tools.SourceCache) -> dict[str, Any]:
    """Run the graph through `kg_validate`, peer and all.

    The peer matters: parity is reciprocal (SCHEMA.md §9), so a write that breaks
    a back-link is only visible with the counterpart graph open. Validating
    without it would accept the one class of damage the check exists for.
    """
    graph = CodeGraph(path)
    try:
        with tools.open_peer(graph) as peer:
            return tools.kg_validate(graph, peer, cache=cache)
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
    out |= {
        f"reference {i['node']} {i['url']} -> {i['narrows_to']}: {i['issue']}"
        for i in report["reference_issues"]
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

    cache = tools.SourceCache()
    before = _validation(target, cache)

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
        copied = hashlib.sha256(tmp.read_bytes()).digest()
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

        after = _validation(tmp, cache)
        new = _new_findings(before, after)
        if new:
            raise EditError(
                "the edit is valid SQL but breaks the graph, so it was rolled back. "
                "New findings: " + "; ".join(new)
            )
        try:
            current = hashlib.sha256(target.read_bytes()).digest()
        except OSError:
            current = None
        if current != copied:
            raise EditError(
                f"{target} changed while the edit was running, so it was not written. "
                "Run the edit again."
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

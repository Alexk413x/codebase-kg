"""Build a `code_graph.db` from nodes. Stdlib only.

The build is **transactional and deterministic**:

- Transactional — everything happens inside one transaction against a temp file
  which is only moved into place on success. A failed or interrupted build
  leaves the committed artifact untouched; there is no truncated-file state.
- Deterministic — fixed page size, sorted insertion order, and a closing VACUUM,
  so rebuilding an unchanged graph produces a byte-identical file. That keeps
  git diffs empty when nothing changed, and lets a caller compare hashes to ask
  "is the committed artifact what this input would produce?".

Validation happens before the write, so failures name the offending node rather
than surfacing as a bare SQLite IntegrityError.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from . import clean
from .models import Meta, Node
from .schema import APPLICATION_ID, DDL, PAGE_SIZE, SCHEMA_VERSION, split_identifier

DanglingPolicy = Literal["error", "drop"]


@dataclass
class BuildReport:
    """What the build did — surfaced by migration and the build/refresh commands."""

    nodes: int = 0
    edges: int = 0
    anchors: int = 0
    dropped_edges: list[tuple[str, str]] = field(default_factory=list)


class BuildError(ValueError):
    """The input cannot produce a valid graph. Nothing was written."""


def _fts_text(node: Node) -> str:
    parts: list[str] = []
    parts += split_identifier(node.id)
    parts += split_identifier(node.kind)
    parts.append(node.description)
    parts.append(node.section)
    for a in node.anchors:
        parts += split_identifier(a.base)
        parts.append(a.path)
        if a.symbol:
            parts += split_identifier(a.symbol)
    return " ".join(p for p in parts if p)


def _validate(
    nodes: list[Node], dangling: DanglingPolicy, report: BuildReport
) -> list[Node]:
    """Check the input and return the nodes that will be written.

    Errors that mean the caller built a broken graph (duplicate ids, empty kind,
    a description that violates the contract) always raise. Dangling edges are
    the one case with a policy, because migration legitimately needs to drop
    edges the old markdown graph accumulated.
    """
    seen: dict[str, Node] = {}
    dupes: set[str] = set()
    for n in nodes:
        if not n.id:
            raise BuildError("node with an empty id")
        if n.id in seen:
            dupes.add(n.id)
        seen[n.id] = n
    if dupes:
        raise BuildError(f"duplicate node ids: {', '.join(sorted(dupes))}")

    ids = set(seen)
    for n in seen.values():
        # The whole node contract in one call, so a violation is reported
        # against the node rather than surfacing later as a CHECK failure with
        # no indication of which row caused it.
        issues = clean.node_problems(n)
        if issues:
            raise BuildError(f"node '{n.id}': {'; '.join(issues)}")

        kept: list[str] = []
        for dst in n.edges:
            if dst == n.id:
                continue  # a self-edge carries no information
            if dst in ids:
                kept.append(dst)
                continue
            if dangling == "error":
                raise BuildError(f"node '{n.id}' has a dangling edge to '{dst}'")
            report.dropped_edges.append((n.id, dst))
        n.edges = sorted(dict.fromkeys(kept))
    return sorted(seen.values(), key=lambda n: n.id)


def _write(conn: sqlite3.Connection, meta: Meta, nodes: list[Node], report: BuildReport) -> None:
    conn.executescript(DDL)

    rows_meta = {
        "schema_version": str(SCHEMA_VERSION),
        "codebase": meta.codebase,
        "root": meta.root,
        "generated": meta.generated,
    }
    if meta.counterpart:
        rows_meta["counterpart"] = meta.counterpart
    if meta.language:
        rows_meta["language"] = meta.language
    rows_meta.update(meta.extra)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        sorted((k, v) for k, v in rows_meta.items() if v),
    )

    conn.executemany(
        "INSERT INTO node (id, kind, description, section, parity, counterpart, divergence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (n.id, n.kind, n.description, n.section, n.parity, n.counterpart, n.divergence)
            for n in nodes
        ],
    )
    conn.executemany(
        "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, ?, ?, ?, ?)",
        [
            (n.id, i, a.path, a.base, a.symbol)
            for n in nodes
            for i, a in enumerate(n.anchors)
        ],
    )
    conn.executemany(
        "INSERT INTO edge (src, dst) VALUES (?, ?)",
        [(n.id, dst) for n in nodes for dst in n.edges],
    )
    conn.executemany(
        "INSERT INTO node_fts (node_id, text) VALUES (?, ?)",
        [(n.id, _fts_text(n)) for n in nodes],
    )

    report.nodes = len(nodes)
    report.anchors = sum(len(n.anchors) for n in nodes)
    report.edges = sum(len(n.edges) for n in nodes)


def build(
    path: str | Path,
    meta: Meta,
    nodes: Iterable[Node],
    *,
    dangling: DanglingPolicy = "error",
) -> BuildReport:
    """Write `nodes` to a fresh `code_graph.db` at `path`, atomically.

    Returns a `BuildReport`. Raises `BuildError` (having written nothing) when
    the input cannot produce a valid graph.
    """
    target = Path(path)
    report = BuildReport()
    ordered = _validate(list(nodes), dangling, report)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    # mkstemp created the file; SQLite wants to initialize it itself.
    tmp.unlink()

    try:
        conn = sqlite3.connect(str(tmp))
        try:
            # Set before any table exists — page_size is fixed at first write and
            # is part of what makes an unchanged rebuild byte-identical.
            conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                _write(conn, meta, ordered, report)
            # Packs the file and drops free pages, so the byte layout depends on
            # the data alone rather than on the order pages happened to be filled.
            conn.execute("VACUUM")
        finally:
            conn.close()
        _replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    return report


def _replace(tmp: Path, target: Path) -> None:
    """`os.replace` with a short retry.

    On Windows a rename over an open file fails outright. The MCP server avoids
    holding the graph open for exactly this reason, but an editor, a file
    indexer or a second session can still have it momentarily; a brief retry
    turns that transient into a non-event instead of a failed rebuild.
    """
    last: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:  # pragma: no cover - Windows-only timing
            last = exc
            time.sleep(0.05 * (attempt + 1))
    assert last is not None
    raise BuildError(
        f"could not replace {target}: {last}. Something else has the file open "
        f"(an editor, a file indexer, or another codebase-kg process)."
    ) from last

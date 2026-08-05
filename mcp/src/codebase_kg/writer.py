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

import hashlib
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from . import clean, links
from .coverage import COVERS_KEY, EXEMPT_KEY, format_patterns
from .models import Meta, Node
from .schema import APPLICATION_ID, DDL, PAGE_SIZE, SCHEMA_VERSION, split_identifier

DanglingPolicy = Literal["error", "drop"]


@dataclass
class BuildReport:
    """What the build did — surfaced by migration and the build/refresh commands."""

    nodes: int = 0
    edges: int = 0
    anchors: int = 0
    hashed: int = 0  # anchors stamped with a source digest
    dropped_edges: list[tuple[str, str]] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)


class BuildError(ValueError):
    """The input cannot produce a valid graph. Nothing was written."""


def fts_text(node: Node) -> str:
    """The search payload for one node.

    Public because `edits.py` rewrites a single FTS row on an upsert. If the two
    sides ever computed this differently, editing a node would silently change
    what finds it — and the next full rebuild would silently change it back.
    """
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


def file_sha(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or None if it cannot be read.

    Bytes, not decoded text: the point is to notice *any* change to the source,
    and decoding with `errors="ignore"` would silently collapse edits inside
    invalid sequences. Read in chunks so a large generated file does not have to
    fit in memory just to be fingerprinted.
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def stamp_hashes(
    nodes: list[Node], base: Path, report: BuildReport, existing: dict[str, str] | None = None
) -> dict[str, str]:
    """Digest every anchored file, keeping any baseline already recorded.

    `existing` wins wherever it has an entry. That is what makes the refresh
    loop honest: export → edit → build carries the old baselines through, so a
    node the author never re-read keeps flagging `changed_since_built` instead of
    being silently re-blessed. Only anchors with no baseline get one, and
    `--rebaseline` is the explicit way to say "I have checked all of these".

    Keeping the old value also preserves the byte-identical round trip: an
    unedited export→build reproduces the same file, so a no-op refresh leaves
    the git diff empty.

    A file that cannot be read simply gets no entry — the graph is still valid,
    that path just has no baseline to compare against.
    """
    digests: dict[str, str] = dict(existing or {})
    for node in nodes:
        for anchor in node.anchors:
            if anchor.path in digests:
                continue
            sha = file_sha(base / anchor.path)
            if sha is None:
                report.missing_sources.append(anchor.path)
            else:
                digests[anchor.path] = sha
    report.missing_sources = sorted(dict.fromkeys(report.missing_sources))
    # Only paths this graph actually anchors — a stale entry for a file no node
    # points at any more would otherwise ride along forever.
    anchored = {a.path for n in nodes for a in n.anchors}
    kept = {p: s for p, s in digests.items() if p in anchored}
    report.hashed = len(kept)
    return kept


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


def _write(
    conn: sqlite3.Connection,
    meta: Meta,
    nodes: list[Node],
    sources: dict[str, str],
    report: BuildReport,
) -> None:
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
    # Stored newline-separated rather than as JSON: the vendored pre-push hook
    # reads these straight out of SQL and deliberately carries no JSON parser.
    if meta.covers:
        rows_meta[COVERS_KEY] = format_patterns(meta.covers)
    if meta.exempt:
        rows_meta[EXEMPT_KEY] = format_patterns(meta.exempt)
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
        "INSERT INTO source (path, sha) VALUES (?, ?)", sorted(sources.items())
    )
    conn.executemany(
        "INSERT INTO edge (src, dst) VALUES (?, ?)",
        [(n.id, dst) for n in nodes for dst in n.edges],
    )
    # Sorted like everything else here, so an unchanged rebuild is byte-identical.
    conn.executemany(
        f"INSERT INTO {links.TABLE} (node_id, target, kind) VALUES (?, ?, ?)",
        [
            (n.id, link.target, link.kind)
            for n in nodes
            for link in links.dedupe(n.links)
        ],
    )
    conn.executemany(
        "INSERT INTO node_fts (node_id, text) VALUES (?, ?)",
        [(n.id, fts_text(n)) for n in nodes],
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
    source_root: str | Path | None = None,
    sources: dict[str, str] | None = None,
    rebaseline: bool = False,
) -> BuildReport:
    """Write `nodes` to a fresh `code_graph.db` at `path`, atomically.

    `sources` are the baselines already on record (from a previous build, via
    export). `source_root` is where anchor paths resolve from; given one, any
    path without a baseline gets hashed. `rebaseline=True` discards the recorded
    baselines and re-hashes everything — say that only when the descriptions
    have actually been re-checked against the code.

    Returns a `BuildReport`. Raises `BuildError` (having written nothing) when
    the input cannot produce a valid graph.
    """
    target = Path(path)
    report = BuildReport()
    ordered = _validate(list(nodes), dangling, report)

    carried = {} if rebaseline else dict(sources or {})
    if source_root is not None:
        resolved = stamp_hashes(ordered, Path(source_root), report, carried)
    else:
        anchored = {a.path for n in ordered for a in n.anchors}
        resolved = {p: s for p, s in carried.items() if p in anchored}
        report.hashed = len(resolved)

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
                _write(conn, meta, ordered, resolved, report)
            # Packs the file and drops free pages, so the byte layout depends on
            # the data alone rather than on the order pages happened to be filled.
            conn.execute("VACUUM")
        finally:
            conn.close()
        replace_file(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    return report


def replace_file(tmp: Path, target: Path) -> None:
    """`os.replace` with a short retry.

    On Windows a rename over an open file fails outright. The MCP server avoids
    holding the graph open for exactly this reason, but an editor, a file
    indexer or a second session can still have it momentarily; a brief retry
    turns that transient into a non-event instead of a failed rebuild.

    Shared with `edits.py`: a targeted write lands the same way a build does,
    so there is one answer to "how does a new graph replace the committed one".
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

"""FastMCP server entry point for codebase-kg.

Eight read-only tools over one repo's `knowledge/code_graph.db`. The graph opens
on each tool call, so tools always see current data — including a graph created
after the server started. The peer graph named in `meta.counterpart` is opened
the same way for the cross-codebase parity checks.

Path resolution order: CLI arg → $CODEBASE_KG_PATH → walk up from CWD honoring
an optional `graph_path` in `.claude/codebase-kg.local.md`, else
`knowledge/code_graph.db` (no repo-root fallback). While unresolved, the path is
re-resolved on every tool call so a freshly built graph is picked up.

**The graph is opened per tool call and closed again.** That is affordable
precisely because opening a store is constant-time (~1 ms) rather than a parse
whose cost grows with the graph — there is nothing to amortize. It also matters
for correctness: a held-open handle blocks Windows from replacing the file, so
caching the connection would make `/codebase-kg:refresh` fail to write its own
output whenever the server was running. Not holding the file means every call
sees current data with no cache-invalidation logic at all.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastmcp import FastMCP

from . import tools as _tools
from .store import CodeGraph, StoreError

GRAPH_FILENAME = "code_graph.db"
# The pre-rewrite artifact. Only ever used to produce a better error message.
LEGACY_FILENAME = "KNOWLEDGE_GRAPH.md"

mcp: FastMCP[Any] = FastMCP("codebase-kg")

# Only the resolved *path* is remembered between calls — never an open handle.
_graph_path: Path | None = None


def _local_graph_path(base: Path) -> Path | None:
    """Honor an optional per-clone path override in `.claude/codebase-kg.local.md`.

    `graph_path` is the current key; `kg_path` is still read so an existing
    checkout keeps working across the rename.
    """
    local = base / ".claude" / "codebase-kg.local.md"
    try:
        if not local.is_file():
            return None
        lines = local.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key not in {"graph_path", "kg_path"}:
            continue
        val = re.sub(r"\s+#.*$", "", val).strip().strip("'\"")
        if val and not val.startswith("<"):
            found[key] = val
    val = found.get("graph_path") or found.get("kg_path")
    if not val:
        return None
    p = Path(val)
    return p if p.is_absolute() else base / p


def _resolve_graph_path() -> Path | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return Path(sys.argv[1]).resolve()
    env = os.environ.get("CODEBASE_KG_PATH")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        override = _local_graph_path(base)
        if override is not None and override.is_file():
            return override.resolve()
        # The graph lives in knowledge/ by convention — no repo-root fallback.
        cand = base / "knowledge" / GRAPH_FILENAME
        if cand.is_file():
            return cand.resolve()
    return None


def _find_legacy() -> Path | None:
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        cand = base / "knowledge" / LEGACY_FILENAME
        if cand.is_file():
            return cand
    return None


def _missing_graph_error() -> FileNotFoundError:
    legacy = _find_legacy()
    if legacy is not None:
        return FileNotFoundError(
            f"Found a pre-rewrite {LEGACY_FILENAME} at {legacy} but no {GRAPH_FILENAME}. "
            f"Migrate it once with:\n"
            f"    python -m codebase_kg.migrate \"{legacy}\"\n"
            f"then commit knowledge/{GRAPH_FILENAME}."
        )
    return FileNotFoundError(
        f"No {GRAPH_FILENAME} found. Run /codebase-kg:build to create one, pass its "
        f"path as the first CLI arg, or set $CODEBASE_KG_PATH."
    )


def _graph_file() -> Path:
    global _graph_path
    if _graph_path is None or not _graph_path.is_file():
        # Re-resolve while unresolved (or if the file went away) — the graph may
        # have been created after the server started (e.g. /codebase-kg:build).
        _graph_path = _resolve_graph_path()
    if _graph_path is None or not _graph_path.is_file():
        raise _missing_graph_error()
    return _graph_path


@contextmanager
def _open_graph() -> Iterator[CodeGraph]:
    """Open the graph for one tool call, then close it."""
    g = CodeGraph(_graph_file())
    try:
        yield g
    finally:
        g.close()


@contextmanager
def _open_peer(g: CodeGraph) -> Iterator[CodeGraph | None]:
    """Open the counterpart graph named in `meta`, if there is a usable one.

    A peer that is absent or unreadable yields None: the parity checks then
    report only what the local half can prove, rather than failing the call.
    """
    cp = g.meta.counterpart
    if not cp:
        yield None
        return
    try:
        peer = CodeGraph((g.path.parent / cp).resolve())
    except StoreError:
        yield None
        return
    try:
        yield peer
    finally:
        peer.close()


@mcp.tool()
def kg_search(query: str, kind: str | None = None) -> dict[str, Any]:
    """Full-text search the code graph for nodes matching a phrase. Use when you
    have a concept but no node id — this is the usual entry point. Ranked, up to
    10 results, each with its `path#Symbol` anchors so you can go straight to the
    code. CamelCase identifiers are matched in split form, so "video playback"
    finds `VideoPlaybackService`.

    - `query`: free-form text (e.g. "feed ranking", "bookmark persistence").
    - `kind`: optional free-text filter on the node's `kind` (e.g. "ViewModel").
    """
    with _open_graph() as g:
        return _tools.kg_search(g, query, kind)


@mcp.tool()
def kg_node(id: str) -> dict[str, Any]:
    """Fetch one full node by id — anchors (`path#Symbol`), description, edges,
    parity, counterpart, plus inbound edges. The primary lookup once you know the
    id. Returns `did_you_mean` suggestions when the id is unknown."""
    with _open_graph() as g:
        return _tools.kg_node(g, id)


@mcp.tool()
def kg_neighborhood(id: str, depth: int = 1) -> dict[str, Any]:
    """Return a node with its graph neighborhood — outbound edges, inbound edges
    (who depends on it), and counterpart — expanded `depth` hops (1–3). Use to
    understand what surrounds a node before changing it."""
    with _open_graph() as g:
        return _tools.kg_neighborhood(g, id, depth)


@mcp.tool()
def kg_find_by_kind(kind: str) -> dict[str, Any]:
    """List every node whose free-text `kind` matches (case-insensitive
    substring) — e.g. all `ViewModel`s, `Service`s, `Room @Entity`s."""
    with _open_graph() as g:
        return _tools.kg_find_by_kind(g, kind)


@mcp.tool()
def kg_find_by_path(path: str) -> dict[str, Any]:
    """Reverse lookup: given a source file, which node(s) own it, and what do
    they connect to. Use when you have a file open and want its place in the map.
    Accepts a repo-relative path or a bare filename (matched as a suffix)."""
    with _open_graph() as g:
        return _tools.kg_find_by_path(g, path)


@mcp.tool()
def kg_parity_gaps(status: str | None = None) -> dict[str, Any]:
    """The cross-codebase gap report, as a query. Lists nodes flagged
    `divergent` or `<codebase>-only`, with their counterpart + divergence line.

    - `status`: optional filter — 'divergent', 'only' (any *-only), or an exact
      flag like 'android-only'.
    """
    with _open_graph() as g:
        return _tools.kg_parity_gaps(g, status)


@mcp.tool()
def kg_stats() -> dict[str, Any]:
    """Counts and health for cold start: node/edge/anchor totals, breakdown by
    kind and section, parity breakdown, isolated nodes, and when the graph was
    generated."""
    with _open_graph() as g:
        return _tools.kg_stats(g)


@mcp.tool()
def kg_validate() -> dict[str, Any]:
    """Advisory drift check against real source (never blocks). Reports anchors
    whose file or symbol no longer exists, source files under the root that no
    node covers, and counterpart problems vs the peer graph. Structural
    integrity — unique ids, no dangling edges, consistent parity — is guaranteed
    by the store and reported rather than checked."""
    with _open_graph() as g, _open_peer(g) as peer:
        return _tools.kg_validate(g, peer)


def main() -> None:
    # The path resolves on the first tool call (see `_graph_file`); doing it
    # here as well changed nothing. The graph opens lazily — so the server still starts
    # cleanly in a repo that has no graph yet (e.g. before /codebase-kg:build).
    # A missing graph surfaces as an actionable error on first use, not as a
    # server that refuses to start.
    mcp.run()


if __name__ == "__main__":
    main()

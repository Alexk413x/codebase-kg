"""FastMCP server entry point for codebase-kg.

Seven read-only tools over one repo's KNOWLEDGE_GRAPH.md. The graph loads
lazily on the first tool call and is re-read whenever the file changes on disk
(mtime/size), so tools always see the current KG — including one created after
the server started. The peer KG named in the header `counterpart:` is loaded
the same way for the cross-codebase parity checks.

KG path resolution order: CLI arg → $CODEBASE_KG_PATH → walk up from CWD
honoring an optional `kg_path` in `.claude/codebase-kg.local.md`, else
knowledge/KNOWLEDGE_GRAPH.md (no repo-root fallback). While unresolved, the
path is re-resolved on every tool call so a freshly built KG is picked up.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from . import tools as _tools
from .loader import load_graph
from .models import Graph

mcp: FastMCP[Any] = FastMCP("codebase-kg")

_graph: Graph | None = None
_graph_sig: tuple[float, int] | None = None  # (mtime, size) of the loaded KG file
_peer: Graph | None = None
_peer_sig: tuple[float, int] | None = None
_graph_path: Path | None = None


def _local_kg_path(base: Path) -> Path | None:
    """Honor an optional per-clone `kg_path` override in
    `.claude/codebase-kg.local.md` (SCHEMA.md §7) — same frontmatter format the
    hooks and the pre-push gate read."""
    local = base / ".claude" / "codebase-kg.local.md"
    try:
        if not local.is_file():
            return None
        lines = local.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        if key.strip().lower() != "kg_path":
            continue
        val = re.sub(r"\s+#.*$", "", val).strip().strip("'\"")
        if not val or val.startswith("<"):
            return None
        p = Path(val)
        return p if p.is_absolute() else base / p
    return None


def _resolve_graph_path() -> Path | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return Path(sys.argv[1]).resolve()
    env = os.environ.get("CODEBASE_KG_PATH")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        # An optional per-clone .claude/codebase-kg.local.md may override kg_path.
        override = _local_kg_path(base)
        if override is not None and override.is_file():
            return override.resolve()
        # The KG lives in knowledge/ by convention — no repo-root fallback.
        cand = base / "knowledge" / "KNOWLEDGE_GRAPH.md"
        if cand.is_file():
            return cand.resolve()
    return None


def _file_sig(p: Path) -> tuple[float, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _get_graph() -> Graph:
    global _graph, _graph_sig, _graph_path, _peer, _peer_sig
    if _graph_path is None or not _graph_path.is_file():
        # Re-resolve while unresolved (or if the file went away) — the KG may
        # have been created after the server started (e.g. /codebase-kg:build).
        _graph_path = _resolve_graph_path()
    if _graph_path is None or not _graph_path.is_file():
        raise FileNotFoundError(
            "No KNOWLEDGE_GRAPH.md found. Pass its path as the first CLI arg "
            "or set $CODEBASE_KG_PATH."
        )
    sig = _file_sig(_graph_path)
    if _graph is None or sig != _graph_sig:
        _graph = load_graph(_graph_path)
        _graph_sig = sig
        # The header may now name a different counterpart — drop the peer
        # cache so it re-resolves on next use.
        _peer = None
        _peer_sig = None
    return _graph


def _get_peer() -> Graph | None:
    """Load the counterpart KG named in the header, if any. Cached, but
    re-read whenever the peer file changes on disk."""
    global _peer, _peer_sig
    g = _get_graph()  # reloading the main KG drops the peer cache too
    cp = g.header.counterpart
    if not cp or not g.path:
        return None
    peer_path = (Path(g.path).parent / cp).resolve()
    sig = _file_sig(peer_path)
    if sig is None:
        _peer = None
        _peer_sig = None
        return None
    if _peer is None or sig != _peer_sig:
        _peer = load_graph(peer_path)
        _peer_sig = sig
    return _peer


@mcp.tool()
def kg_search(query: str, kind: str | None = None) -> dict[str, Any]:
    """Find nodes by token match on id + kind + summary + anchors. Use when you
    have a phrase but no node id. Returns up to 10 ranked matches.

    - `query`: free-form text (e.g. "feed ranking", "bookmark persistence").
    - `kind`: optional free-text filter on the node's `kind` (e.g. "ViewModel").
    """
    return _tools.kg_search(_get_graph(), query, kind)


@mcp.tool()
def kg_node(id: str) -> dict[str, Any]:
    """Fetch one full node by id — anchors (`path#Symbol`), summary, edges,
    parity, counterpart, divergence, plus inbound edges. The primary lookup once
    you know the id. Returns `did_you_mean` suggestions when the id is unknown."""
    return _tools.kg_node(_get_graph(), id)


@mcp.tool()
def kg_neighborhood(id: str, depth: int = 1) -> dict[str, Any]:
    """Return a node with its graph neighborhood — outbound edges, inbound edges
    (who depends on it), and counterpart — expanded `depth` hops (1–2). Use to
    understand what surrounds a node before changing it."""
    return _tools.kg_neighborhood(_get_graph(), id, depth)


@mcp.tool()
def kg_find_by_kind(kind: str) -> dict[str, Any]:
    """List every node whose free-text `kind` matches (case-insensitive
    substring) — e.g. all `ViewModel`s, `Service`s, `Room @Entity`s."""
    return _tools.kg_find_by_kind(_get_graph(), kind)


@mcp.tool()
def kg_parity_gaps(status: str | None = None) -> dict[str, Any]:
    """The cross-codebase gap report, as a query. Lists nodes flagged
    `divergent` or `<codebase>-only`, with their counterpart + divergence line.

    - `status`: optional filter — 'divergent', 'only' (any *-only), or an exact
      flag like 'android-only'.
    """
    return _tools.kg_parity_gaps(_get_graph(), status)


@mcp.tool()
def kg_stats() -> dict[str, Any]:
    """Counts and health for cold start: node/edge totals, breakdown by kind and
    section, parity breakdown, and the last `refreshed` date."""
    return _tools.kg_stats(_get_graph())


@mcp.tool()
def kg_validate() -> dict[str, Any]:
    """Advisory drift check (never blocks). Reports duplicate node ids, dangling
    edges, counterpart problems (missing target / not reciprocal with the peer
    KG), parity field inconsistencies, and ungreppable anchors (a `path#Symbol`
    whose symbol no longer appears in source — the strongest staleness signal)."""
    return _tools.kg_validate(_get_graph(), _get_peer())


def main() -> None:
    global _graph_path
    _graph_path = _resolve_graph_path()
    # The graph loads lazily on the first tool call — so the server still starts
    # cleanly in a repo that has no KNOWLEDGE_GRAPH.md yet (e.g. before the user
    # runs /codebase-kg:build). A missing KG surfaces as a clear error on first
    # use, not as a server that refuses to start — and once the file exists (or
    # changes), the next tool call picks it up automatically.
    mcp.run()


if __name__ == "__main__":
    main()

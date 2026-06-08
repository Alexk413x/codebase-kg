"""FastMCP server entry point for codebase-kg.

Seven read-only tools over one repo's KNOWLEDGE_GRAPH.md (parsed once at start).
The peer KG named in the header `counterpart:` is loaded lazily for the
cross-codebase parity checks.

KG path resolution order: CLI arg → $CODEBASE_KG_PATH → walk up from CWD for a
KNOWLEDGE_GRAPH.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from . import tools as _tools
from .loader import load_graph
from .models import Graph

mcp: FastMCP[Any] = FastMCP("codebase-kg")

_graph: Graph | None = None
_peer: Graph | None = None
_peer_loaded = False
_graph_path: Path | None = None


def _resolve_graph_path() -> Path | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return Path(sys.argv[1]).resolve()
    env = os.environ.get("CODEBASE_KG_PATH")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        for cand in (base / "KNOWLEDGE_GRAPH.md", base / "knowledge" / "KNOWLEDGE_GRAPH.md"):
            if cand.is_file():
                return cand.resolve()
    return None


def _get_graph() -> Graph:
    global _graph
    if _graph is None:
        if _graph_path is None or not _graph_path.is_file():
            raise FileNotFoundError(
                "No KNOWLEDGE_GRAPH.md found. Pass its path as the first CLI arg "
                "or set $CODEBASE_KG_PATH."
            )
        _graph = load_graph(_graph_path)
    return _graph


def _get_peer() -> Graph | None:
    """Lazy-load the counterpart KG named in the header, if any."""
    global _peer, _peer_loaded
    if _peer_loaded:
        return _peer
    _peer_loaded = True
    g = _get_graph()
    cp = g.header.counterpart
    if cp and g.path:
        peer_path = (Path(g.path).parent / cp).resolve()
        if peer_path.is_file():
            _peer = load_graph(peer_path)
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
    """Advisory drift check (never blocks). Reports dangling edges, counterpart
    problems (missing target / not reciprocal with the peer KG), parity field
    inconsistencies, and ungreppable anchors (a `path#Symbol` whose symbol no
    longer appears in source — the strongest staleness signal)."""
    return _tools.kg_validate(_get_graph(), _get_peer())


def main() -> None:
    global _graph_path
    _graph_path = _resolve_graph_path()
    # Fail fast with a clear message if the KG is missing.
    _get_graph()
    mcp.run()


if __name__ == "__main__":
    main()

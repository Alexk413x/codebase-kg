"""Read-only queries over a `CodeGraph`. Stdlib only.

Each function takes a `CodeGraph` (and sometimes a peer graph for the
cross-codebase parity checks) and returns a JSON-serializable dict.
`server.py` wraps these as FastMCP tools.

These are SQL queries, not scans. Search goes through the persisted FTS5 index,
lookups go through the primary key, "who points at this?" goes through the
`edge_dst` index, and "which node owns this file?" goes through `anchor_path` —
so cost tracks the size of the answer rather than the size of the graph.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import clean
from .models import Anchor, Node
from .store import CodeGraph, tokenize

# Directories that are never source (mirrors the pre-push gate's list).
IGNORE_DIRS = {
    ".git", ".github", ".githooks", ".claude", "node_modules", "build", "dist",
    "out", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode",
    "target", "Pods", "DerivedData", ".next", "vendor",
}
# Precomputed once — this was being rebuilt for every file in the tree walk.
_IGNORE_LOWER = {d.lower() for d in IGNORE_DIRS}


# --------------------------------------------------------------------------- #
# kg_search
# --------------------------------------------------------------------------- #
def kg_search(
    graph: CodeGraph, query: str, kind: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """Ranked full-text search over id, kind, description and anchors."""
    # Over-fetch so a `kind` filter still has candidates left to rank.
    hits = graph.search_ids(query, limit * 5 if kind else limit * 2)
    if not hits:
        return {"query": query, "count": 0, "results": []}
    nodes = {n.id: n for n in graph.nodes([nid for nid, _ in hits], with_edges=False)}

    q = query.lower().strip()
    q_tokens = tokenize(query)
    # (-score, id, payload) so the sort key stays typed instead of being dug
    # back out of the JSON dict.
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for node_id, score in hits:
        n = nodes.get(node_id)
        if n is None:
            continue
        if kind is not None and kind.lower() not in n.kind.lower():
            continue
        # bm25 alone under-ranks the node whose *name* is the query, because on
        # documents this short a term in the description scores about the same
        # as a term in the id. Boost by how much of the query the id accounts
        # for, so "quartz upload" puts `quartz_upload_worker` above a node that
        # merely mentions quartz.
        if q and q in n.id.lower():
            score += 10.0
        elif q_tokens:
            id_tokens = set(tokenize(n.id))
            overlap = sum(1 for t in q_tokens if t in id_tokens)
            if overlap:
                score += 6.0 * overlap / len(q_tokens)
        scored.append(
            (
                -score,
                n.id,
                {
                    "id": n.id,
                    "kind": n.kind,
                    "section": n.section,
                    "description": n.description,
                    "anchors": [str(a) for a in n.anchors],
                    "parity": n.parity,
                    "score": round(score, 4),
                },
            )
        )
    scored.sort(key=lambda s: (s[0], s[1]))
    results = [payload for _, _, payload in scored]
    return {"query": query, "count": len(results), "results": results[:limit]}


# --------------------------------------------------------------------------- #
# kg_node
# --------------------------------------------------------------------------- #
def kg_node(graph: CodeGraph, id: str) -> dict[str, Any]:
    """Full node by id."""
    n = graph.node(id)
    if n is None:
        return {"found": False, "id": id, "did_you_mean": graph.similar_ids(id)}
    out = n.to_dict()
    out["found"] = True
    out["inbound_edges"] = graph.inbound(id)
    return out


# --------------------------------------------------------------------------- #
# kg_neighborhood
# --------------------------------------------------------------------------- #
def kg_neighborhood(graph: CodeGraph, id: str, depth: int = 1) -> dict[str, Any]:
    """A node plus everything within `depth` hops, following edges either way."""
    center = graph.node(id)
    if center is None:
        return {"found": False, "id": id, "did_you_mean": graph.similar_ids(id)}
    depth = max(1, min(depth, 3))
    hops = graph.neighborhood_ids(id, depth)
    hops.pop(id, None)
    nodes = {n.id: n for n in graph.nodes(list(hops), with_edges=False)}
    neighbors = [
        {
            "id": n.id,
            "kind": n.kind,
            "description": n.description,
            "anchors": [str(a) for a in n.anchors],
            "hops": hops[n.id],
        }
        for n in (nodes[i] for i in hops if i in nodes)
    ]
    neighbors.sort(key=lambda r: (int(r["hops"]), str(r["id"])))
    return {
        "found": True,
        "center": center.to_dict(),
        "depth": depth,
        "inbound_edges": graph.inbound(id),
        "count": len(neighbors),
        "neighbors": neighbors,
        "counterpart": center.counterpart,
    }


# --------------------------------------------------------------------------- #
# kg_find_by_kind
# --------------------------------------------------------------------------- #
def kg_find_by_kind(graph: CodeGraph, kind: str) -> dict[str, Any]:
    """All nodes whose free-text `kind` matches (case-insensitive substring)."""
    matches = [
        {
            "id": n.id,
            "kind": n.kind,
            "description": n.description,
            "anchors": [str(a) for a in n.anchors],
        }
        for n in graph.by_kind(kind)
    ]
    return {"kind": kind, "count": len(matches), "nodes": matches}


# --------------------------------------------------------------------------- #
# kg_find_by_path
# --------------------------------------------------------------------------- #
def kg_find_by_path(graph: CodeGraph, path: str) -> dict[str, Any]:
    """Reverse lookup: which node(s) own a source file.

    The inverse of every other tool here — you have a file open and want its
    place in the map. An indexed lookup on `anchor.path`, so it stays cheap on
    a large graph. Matches a bare filename as a path suffix.
    """
    found = graph.by_path(path)
    # One query for every match's inbound edges, rather than one per match —
    # a bare filename can legitimately hit many nodes.
    inbound = graph.inbound_many([n.id for n, _ in found])
    return {
        "path": path,
        "count": len(found),
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "description": n.description,
                "matched_anchors": [str(a) for a in anchors],
                "edges": n.edges,
                "inbound_edges": inbound.get(n.id, []),
            }
            for n, anchors in found
        ],
    }


# --------------------------------------------------------------------------- #
# kg_parity_gaps
# --------------------------------------------------------------------------- #
def kg_parity_gaps(graph: CodeGraph, status: str | None = None) -> dict[str, Any]:
    """Nodes flagged `divergent` or `<codebase>-only` — the gap report as a query.

    `status` optionally filters: 'divergent', 'only' (any *-only), or an exact
    flag like 'android-only'.
    """
    gaps: list[dict[str, Any]] = []
    wanted = status.lower() if status is not None else None
    for n in graph.parity_nodes():
        assert n.parity is not None  # parity_nodes() only returns flagged nodes
        if wanted is not None:
            s = wanted
            if s == "only":
                if not n.parity.endswith("-only"):
                    continue
            elif s != n.parity:
                continue
        gaps.append(
            {
                "id": n.id,
                "kind": n.kind,
                "parity": n.parity,
                "counterpart": n.counterpart,
                "divergence": n.divergence,
                "description": n.description,
            }
        )
    breakdown: dict[str, int] = {}
    for g in gaps:
        breakdown[str(g["parity"])] = breakdown.get(str(g["parity"]), 0) + 1
    return {"count": len(gaps), "by_status": breakdown, "gaps": gaps}


# --------------------------------------------------------------------------- #
# kg_stats
# --------------------------------------------------------------------------- #
def kg_stats(graph: CodeGraph) -> dict[str, Any]:
    meta = graph.meta
    counts = graph.counts()
    isolated = graph.isolated_ids(limit=20)
    isolated_total = graph.isolated_count()
    return {
        "codebase": meta.codebase,
        "root": meta.root,
        "language": meta.language,
        "counterpart": meta.counterpart,
        "generated": meta.generated,
        "nodes": counts["nodes"],
        "edges": counts["edges"],
        "anchors": counts["anchors"],
        "files_anchored": counts["files"],
        "kinds": graph.group_counts("kind"),
        "sections": graph.group_counts("section"),
        "parity": {k: v for k, v in graph.group_counts("parity").items() if k != "(none)"},
        "isolated_nodes": {"count": isolated_total, "ids": isolated},
    }


# --------------------------------------------------------------------------- #
# kg_validate  (the advisory drift detector)
# --------------------------------------------------------------------------- #
def _resolve_source_base(graph: CodeGraph, repo_root: str | None) -> Path | None:
    """Find the directory anchor paths are relative to.

    Tries the explicit repo_root, then the meta `root` joined onto a few
    candidate bases, and picks whichever resolves the most anchors.
    """
    rels = graph.anchor_paths()
    if not rels:
        return None
    root = graph.meta.root.strip().rstrip("/")
    candidates: list[Path] = []
    if repo_root:
        rp = Path(repo_root)
        candidates += [rp / root, rp]
    graph_dir = graph.path.parent
    candidates += [graph_dir / root, graph_dir.parent / root, graph_dir, graph_dir.parent]
    best: tuple[int, Path] | None = None
    for base in candidates:
        hits = sum(1 for r in rels[:25] if (base / r).is_file())
        if hits > 0 and (best is None or hits > best[0]):
            best = (hits, base)
    return best[1] if best else None


def _word_pattern(segment: str, _cache: dict[str, re.Pattern[str]] = {}) -> re.Pattern[str]:
    """A compiled `\\bsegment\\b` matcher, memoized.

    `re`'s internal cache holds 512 patterns; a graph with more distinct symbols
    than that recompiles on every anchor. An explicit memo keeps the hot loop of
    `kg_validate` compiling each symbol once.
    """
    pattern = _cache.get(segment)
    if pattern is None:
        pattern = _cache[segment] = re.compile(r"\b" + re.escape(segment) + r"\b")
    return pattern


def _symbol_in_source(symbol: str, src: str) -> bool:
    """True when the anchor's symbol appears in the source.

    A `Type.method` anchor (SCHEMA.md §4.1) rarely appears literally — check
    each dotted segment on its own word boundary instead.
    """
    for seg in symbol.split("."):
        if not seg:
            continue
        # Plain substring first: it is far cheaper than a regex and rules out
        # the common miss without touching the pattern cache at all.
        if seg not in src or not _word_pattern(seg).search(src):
            return False
    return True


def _check_anchors(graph: CodeGraph, base: Path) -> tuple[list[dict[str, str]], int]:
    """Every anchor against real source. The strongest staleness signal there is.

    `all_anchors()` yields in path order, so each file is stat-ed and read once
    and only the current file's text is held — rather than caching the whole
    source tree in memory to avoid re-reads.
    """
    issues: list[dict[str, str]] = []
    checked = 0
    current: str | None = None
    src: str | None = None
    exists = False
    for node_id, anchor in graph.all_anchors():
        checked += 1
        if anchor.path != current:
            current, src = anchor.path, None
            fp = base / anchor.path
            exists = fp.is_file()
            if exists:
                try:
                    src = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    src = None
        if not exists:
            issues.append({"node": node_id, "anchor": str(anchor), "issue": "file not found"})
            continue
        if anchor.symbol and src is not None and not _symbol_in_source(anchor.symbol, src):
            issues.append(
                {"node": node_id, "anchor": str(anchor), "issue": "symbol not found in file"}
            )
    return issues, checked


def uncovered_sources(graph: CodeGraph, base: Path, limit: int = 50) -> list[str]:
    """Source files under the root that no node anchors on.

    Language-agnostic by construction: the "source extensions" for a repo are
    whatever extensions the graph already anchors on. A repo of Kotlin nodes
    looks for `.kt`; a TypeScript one looks for `.ts`. Nothing is hardcoded.
    """
    anchored = set(graph.anchor_paths())  # already posix-normalized by the store
    exts = {Path(p).suffix.lower() for p in anchored if Path(p).suffix}
    if not exts:
        return []
    missing: list[str] = []
    # os.walk with in-place pruning, not rglob: rglob descends into
    # node_modules/.git/build in full and only filters afterwards, so the
    # ignored 99% of a real tree gets walked and sorted before being discarded.
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in _IGNORE_LOWER]
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in exts:
                continue
            rel = prefix + name
            if rel not in anchored:
                missing.append(rel)
                if len(missing) >= limit:
                    return missing
    return missing


def kg_validate(
    graph: CodeGraph, peer: CodeGraph | None = None, repo_root: str | None = None
) -> dict[str, Any]:
    """Advisory drift check against real source. Never blocks anything.

    Note what is *absent* from the report. Duplicate ids, dangling edges and
    inconsistent parity triples were the bulk of the old check; they are now
    impossible to write (schema.py), so they are reported as guarantees rather
    than searched for. What remains is everything the file itself cannot know:
    whether the anchors still point at real code, whether new source appeared
    that nobody mapped, and whether the peer graph agrees.
    """
    counterpart_issues: list[dict[str, str]] = []
    description_issues: list[dict[str, str]] = []

    # `node_summaries` reads the three columns this loop touches; hydrating
    # every node's anchors and edges to look at three strings is the bulk of
    # what this check used to cost on a large graph.
    resolved: dict[str, bool] = {}  # cp_path -> file exists (the peer repeats)
    for node_id, description, counterpart in graph.node_summaries():
        for issue in clean.problems(description):
            description_issues.append({"node": node_id, "issue": issue})
        if counterpart:
            counterpart_issues += _check_counterpart(
                graph, node_id, counterpart, peer, resolved
            )

    base = _resolve_source_base(graph, repo_root)
    anchor_issues: list[dict[str, str]] = []
    uncovered: list[str] = []
    checked = 0
    if base is not None:
        anchor_issues, checked = _check_anchors(graph, base)
        uncovered = uncovered_sources(graph, base)

    ok = not (counterpart_issues or description_issues or anchor_issues or uncovered)
    return {
        "ok": ok,
        "advisory": True,
        "source_checked": base is not None,
        "source_base": str(base) if base else None,
        "anchors_checked": checked,
        "anchor_issues": anchor_issues,
        "uncovered_sources": {"count": len(uncovered), "files": uncovered},
        "counterpart_issues": counterpart_issues,
        "description_issues": description_issues,
        "guaranteed_by_schema": [
            "unique node ids (primary key)",
            "no dangling edges (foreign key)",
            "no orphan anchors (foreign key)",
            "consistent parity/counterpart/divergence (check constraints)",
            "description length + shape (check constraint + writer)",
        ],
    }


def _check_counterpart(
    graph: CodeGraph,
    node_id: str,
    counterpart: str,
    peer: CodeGraph | None,
    resolved: dict[str, bool],
) -> list[dict[str, str]]:
    cp_path, _, cp_id = counterpart.partition("#")
    issues: list[dict[str, str]] = []
    if not cp_id:
        return [{"node": node_id, "issue": "counterpart missing #node-id"}]
    # Every node in a paired graph names the same peer file; resolving it is a
    # syscall, so remember the answer rather than asking once per node.
    exists = resolved.get(cp_path)
    if exists is None:
        exists = resolved[cp_path] = (graph.path.parent / cp_path).resolve().is_file()
    if not exists:
        return [{"node": node_id, "issue": f"counterpart file not found: {cp_path}"}]
    if peer is None:
        return issues
    peer_node = peer.node(cp_id)
    if peer_node is None:
        issues.append({"node": node_id, "issue": f"counterpart id '{cp_id}' not in peer graph"})
    elif not peer_node.counterpart:
        issues.append(
            {"node": node_id, "issue": f"peer '{cp_id}' has no back-link (not reciprocal)"}
        )
    else:
        back_id = peer_node.counterpart.partition("#")[2]
        if back_id != node_id:
            issues.append(
                {"node": node_id, "issue": f"not reciprocal — peer '{cp_id}' links to '{back_id}'"}
            )
    return issues


__all__ = [
    "kg_search",
    "kg_node",
    "kg_neighborhood",
    "kg_find_by_kind",
    "kg_find_by_path",
    "kg_parity_gaps",
    "kg_stats",
    "kg_validate",
    "uncovered_sources",
    "Anchor",
]

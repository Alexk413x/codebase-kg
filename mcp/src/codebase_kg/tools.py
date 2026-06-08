"""Read-only queries over a parsed `Graph`. Stdlib only.

Each function takes a `Graph` (and sometimes an optional peer `Graph` for the
cross-codebase parity checks) and returns a JSON-serializable dict. `server.py`
wraps these as FastMCP tools.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Graph, Node

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text)]


# --------------------------------------------------------------------------- #
# kg_search
# --------------------------------------------------------------------------- #
def kg_search(
    graph: Graph, query: str, kind: str | None = None, limit: int = 10
) -> dict[str, object]:
    """Token search over id + kind + summary. Ranked, capped."""
    q = _tokens(query)
    results: list[dict[str, object]] = []
    for n in graph.nodes:
        if kind is not None and kind.lower() not in n.kind.lower():
            continue
        hay = " ".join([n.id, n.kind, n.summary, " ".join(n.anchors)]).lower()
        score = sum(1 for t in set(q) if t in hay)
        # exact id/substring boosts
        if query.lower() in n.id.lower():
            score += 3
        if score <= 0:
            continue
        results.append(
            {
                "id": n.id,
                "kind": n.kind,
                "section": n.section,
                "summary": _truncate(n.summary, 240),
                "parity": n.parity,
                "score": score,
            }
        )
    results.sort(key=lambda r: (-int(r["score"]), str(r["id"])))
    return {"query": query, "count": len(results), "results": results[:limit]}


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# kg_node
# --------------------------------------------------------------------------- #
def kg_node(graph: Graph, id: str) -> dict[str, object]:
    """Full node by id."""
    n = graph.by_id(id)
    if n is None:
        return {"found": False, "id": id, "did_you_mean": _suggest(graph, id)}
    out = n.to_dict()
    out["found"] = True
    out["inbound_edges"] = graph.inbound(id)
    return out


def _suggest(graph: Graph, id: str, limit: int = 5) -> list[str]:
    q = id.lower()
    scored = [(sum(t in nid.lower() for t in _tokens(q)) + (q in nid.lower()), nid) for nid in graph.ids]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(reverse=True)
    return [nid for _, nid in scored[:limit]]


# --------------------------------------------------------------------------- #
# kg_neighborhood
# --------------------------------------------------------------------------- #
def kg_neighborhood(graph: Graph, id: str, depth: int = 1) -> dict[str, object]:
    """A node plus its edges (out + in) and counterpart, expanded N hops (max 2)."""
    if graph.by_id(id) is None:
        return {"found": False, "id": id, "did_you_mean": _suggest(graph, id)}
    depth = max(1, min(depth, 2))
    seen: set[str] = {id}
    frontier: set[str] = {id}
    for _ in range(depth):
        nxt: set[str] = set()
        for nid in frontier:
            node = graph.by_id(nid)
            if node is None:
                continue
            for e in node.edges:
                if e not in seen:
                    nxt.add(e)
            for inb in graph.inbound(nid):
                if inb not in seen:
                    nxt.add(inb)
        seen |= nxt
        frontier = nxt
        if not frontier:
            break

    center = graph.by_id(id)
    assert center is not None
    neighbors: list[dict[str, object]] = []
    for nid in sorted(seen - {id}):
        n = graph.by_id(nid)
        if n is None:
            neighbors.append({"id": nid, "resolved": False})  # dangling edge target
            continue
        neighbors.append(
            {
                "id": n.id,
                "kind": n.kind,
                "summary": _truncate(n.summary, 160),
                "resolved": True,
            }
        )
    return {
        "found": True,
        "center": center.to_dict(),
        "depth": depth,
        "neighbors": neighbors,
        "counterpart": center.counterpart,
    }


# --------------------------------------------------------------------------- #
# kg_find_by_kind
# --------------------------------------------------------------------------- #
def kg_find_by_kind(graph: Graph, kind: str) -> dict[str, object]:
    """All nodes whose free-text `kind` matches (case-insensitive substring)."""
    k = kind.lower()
    matches = [
        {"id": n.id, "kind": n.kind, "summary": _truncate(n.summary, 160)}
        for n in graph.nodes
        if k in n.kind.lower()
    ]
    return {"kind": kind, "count": len(matches), "nodes": matches}


# --------------------------------------------------------------------------- #
# kg_parity_gaps
# --------------------------------------------------------------------------- #
def _is_gap(parity: str | None) -> bool:
    return parity is not None and (parity == "divergent" or parity.endswith("-only"))


def kg_parity_gaps(graph: Graph, status: str | None = None) -> dict[str, object]:
    """Nodes flagged `divergent` or `<codebase>-only` — the gap report as a query.

    `status` optionally filters: 'divergent', 'only' (any *-only), or an exact
    flag like 'android-only'.
    """
    gaps: list[dict[str, object]] = []
    for n in graph.nodes:
        if not _is_gap(n.parity):
            continue
        if status is not None:
            s = status.lower()
            assert n.parity is not None
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
                "summary": _truncate(n.summary, 200),
            }
        )
    breakdown: dict[str, int] = {}
    for g in gaps:
        breakdown[str(g["parity"])] = breakdown.get(str(g["parity"]), 0) + 1
    return {"count": len(gaps), "by_status": breakdown, "gaps": gaps}


# --------------------------------------------------------------------------- #
# kg_stats
# --------------------------------------------------------------------------- #
def kg_stats(graph: Graph) -> dict[str, object]:
    kinds: dict[str, int] = {}
    parity: dict[str, int] = {}
    sections: dict[str, int] = {}
    edge_count = 0
    for n in graph.nodes:
        kinds[n.kind or "(none)"] = kinds.get(n.kind or "(none)", 0) + 1
        sections[n.section or "(none)"] = sections.get(n.section or "(none)", 0) + 1
        edge_count += len(n.edges)
        if n.parity:
            parity[n.parity] = parity.get(n.parity, 0) + 1
    return {
        "codebase": graph.header.codebase,
        "refreshed": graph.header.refreshed,
        "counterpart": graph.header.counterpart,
        "nodes": len(graph.nodes),
        "edges": edge_count,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "parity": parity,
        "sections": sections,
    }


# --------------------------------------------------------------------------- #
# kg_validate  (the advisory drift detector)
# --------------------------------------------------------------------------- #
def _resolve_source_base(graph: Graph, repo_root: str | None) -> Path | None:
    """Find the directory that anchor paths are relative to. Tries the explicit
    repo_root, then the header `root` joined onto a few candidate bases."""
    rels = [a.split("#", 1)[0] for n in graph.nodes for a in n.anchors]
    rels = [r for r in rels if r]
    if not rels:
        return None
    root = graph.header.root.strip().strip("/")
    candidates: list[Path] = []
    if repo_root:
        rp = Path(repo_root)
        candidates += [rp / root, rp]
    if graph.path:
        kg_dir = Path(graph.path).parent
        candidates += [kg_dir / root, kg_dir.parent / root, kg_dir, kg_dir.parent]
    best: tuple[int, Path] | None = None
    for base in candidates:
        hits = sum(1 for r in rels[:25] if (base / r).is_file())
        if hits > 0 and (best is None or hits > best[0]):
            best = (hits, base)
    return best[1] if best else None


def kg_validate(
    graph: Graph, peer: Graph | None = None, repo_root: str | None = None
) -> dict[str, object]:
    """Deterministic drift checks. Advisory — never blocks anything.

    Reports: dangling edges, counterpart problems (missing target / not
    reciprocal), parity/counterpart field inconsistencies, and ungreppable
    anchors (symbol no longer in the source file).
    """
    dangling_edges: list[dict[str, str]] = []
    field_issues: list[dict[str, str]] = []
    counterpart_issues: list[dict[str, str]] = []

    ids = graph.ids
    for n in graph.nodes:
        for e in n.edges:
            if e not in ids:
                dangling_edges.append({"node": n.id, "edge": e})

        # parity/counterpart consistency (SCHEMA.md §8)
        p = n.parity
        if p == "divergent":
            if not n.counterpart:
                field_issues.append({"node": n.id, "issue": "parity=divergent but no counterpart"})
            if not n.divergence:
                field_issues.append({"node": n.id, "issue": "parity=divergent but no divergence line"})
        elif p and p.endswith("-only"):
            if n.counterpart:
                field_issues.append({"node": n.id, "issue": f"parity={p} should not have a counterpart"})
        elif p == "matched":
            if not n.counterpart:
                field_issues.append({"node": n.id, "issue": "parity=matched but no counterpart"})
        if n.counterpart and not p:
            field_issues.append({"node": n.id, "issue": "counterpart set but no parity flag"})

        # counterpart resolution / reciprocity
        if n.counterpart:
            counterpart_issues += _check_counterpart(graph, n, peer)

    # anchor symbol check (needs source)
    anchor_issues: list[dict[str, str]] = []
    checked = 0
    base = _resolve_source_base(graph, repo_root)
    if base is not None:
        for n in graph.nodes:
            for a in n.anchors:
                rel, _, symbol = a.partition("#")
                if not rel:
                    continue
                fp = base / rel
                checked += 1
                if not fp.is_file():
                    anchor_issues.append({"node": n.id, "anchor": a, "issue": "file not found"})
                    continue
                if symbol:
                    try:
                        src = fp.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    if not re.search(r"\b" + re.escape(symbol) + r"\b", src):
                        anchor_issues.append(
                            {"node": n.id, "anchor": a, "issue": "symbol not found in file"}
                        )

    ok = not (dangling_edges or field_issues or counterpart_issues or anchor_issues)
    return {
        "ok": ok,
        "advisory": True,
        "source_checked": base is not None,
        "source_base": str(base) if base else None,
        "anchors_checked": checked,
        "dangling_edges": dangling_edges,
        "counterpart_issues": counterpart_issues,
        "field_issues": field_issues,
        "anchor_issues": anchor_issues,
    }


def _check_counterpart(graph: Graph, n: Node, peer: Graph | None) -> list[dict[str, str]]:
    assert n.counterpart is not None
    cp_path, _, cp_id = n.counterpart.partition("#")
    issues: list[dict[str, str]] = []
    if not cp_id:
        issues.append({"node": n.id, "issue": "counterpart missing #node-id"})
        return issues
    # resolve the counterpart file relative to this KG's directory
    if graph.path:
        target = (Path(graph.path).parent / cp_path).resolve()
        if not target.is_file():
            issues.append({"node": n.id, "issue": f"counterpart file not found: {cp_path}"})
            return issues
    if peer is not None:
        peer_node = peer.by_id(cp_id)
        if peer_node is None:
            issues.append({"node": n.id, "issue": f"counterpart id '{cp_id}' not in peer KG"})
        elif peer_node.counterpart:
            back_id = peer_node.counterpart.partition("#")[2]
            if back_id != n.id:
                issues.append(
                    {"node": n.id, "issue": f"not reciprocal — peer '{cp_id}' links to '{back_id}'"}
                )
        else:
            issues.append({"node": n.id, "issue": f"peer '{cp_id}' has no back-link (not reciprocal)"})
    return issues

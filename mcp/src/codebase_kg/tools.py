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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from . import clean, staleness
from .coverage import CoverageReport, classify, declared_roots, resolve_source_base
from .links import Resolution, resolve
from .models import Anchor
from .store import CodeGraph, StoreError, tokenize
from .writer import file_sha

# Directories that are never source (mirrors the pre-push gate's list).
IGNORE_DIRS = {
    ".git", ".github", ".githooks", ".claude", "node_modules", "build", "dist",
    "out", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode",
    "target", "Pods", "DerivedData", ".next", "vendor",
}
# Precomputed once — this was being rebuilt for every file in the tree walk.
_IGNORE_LOWER = {d.lower() for d in IGNORE_DIRS}

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def _page(
    items: list[Any], limit: int, offset: int, narrow: str
) -> tuple[list[Any], dict[str, Any]]:
    offset = max(0, offset)
    page = items[offset : offset + max(1, limit)]
    rest = len(items) - offset - len(page)
    info: dict[str, Any] = {"total": len(items), "truncated": rest > 0}
    if rest > 0:
        info["next_offset"] = offset + len(page)
        info["hint"] = (
            f"{rest} more not shown. {narrow}, or pass offset={offset + len(page)} "
            f"for the next page, or a larger limit (up to {MAX_LIMIT})."
        )
    return page, info


@contextmanager
def open_peer(graph: CodeGraph) -> Iterator[CodeGraph | None]:
    """Open the counterpart graph named in `meta`, if there is a usable one.

    A peer that is absent or unreadable yields None: the parity checks then
    report only what the local half can prove, rather than failing the call.

    Lives here rather than in `server.py` because `edits.py` needs the same peer
    the validation tool would have used — a write judged against a validation
    run that skipped the peer would accept a counterpart the peer never
    reciprocates.
    """
    cp = graph.meta.counterpart
    if not cp:
        yield None
        return
    try:
        peer = CodeGraph((graph.path.parent / cp).resolve())
    except StoreError:
        yield None
        return
    try:
        yield peer
    finally:
        peer.close()


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
def kg_neighborhood(
    graph: CodeGraph, id: str, depth: int = 1, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> dict[str, Any]:
    """A node plus everything within `depth` hops, following edges either way."""
    center = graph.node(id)
    if center is None:
        return {"found": False, "id": id, "did_you_mean": graph.similar_ids(id)}
    depth = max(1, min(depth, 3))
    hops = graph.neighborhood_ids(id, depth)
    hops.pop(id, None)
    ordered = sorted(hops, key=lambda i: (hops[i], i))
    ids, paging = _page(ordered, limit, offset, "Pass a smaller depth")
    nodes = {n.id: n for n in graph.nodes(ids, with_edges=False)}
    neighbors = [
        {
            "id": n.id,
            "kind": n.kind,
            "description": n.description,
            "anchors": [str(a) for a in n.anchors],
            "hops": hops[n.id],
        }
        for n in (nodes[i] for i in ids if i in nodes)
    ]
    return {
        "found": True,
        "center": center.to_dict(),
        "depth": depth,
        "inbound_edges": graph.inbound(id),
        "count": len(neighbors),
        **paging,
        "neighbors": neighbors,
        "counterpart": center.counterpart,
    }


# --------------------------------------------------------------------------- #
# kg_find_by_kind
# --------------------------------------------------------------------------- #
def kg_find_by_kind(
    graph: CodeGraph, kind: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> dict[str, Any]:
    """All nodes whose free-text `kind` matches (case-insensitive substring)."""
    found, paging = _page(graph.by_kind(kind), limit, offset, "Pass a more specific kind")
    matches = [
        {
            "id": n.id,
            "kind": n.kind,
            "description": n.description,
            "anchors": [str(a) for a in n.anchors],
        }
        for n in found
    ]
    return {"kind": kind, "count": len(matches), **paging, "nodes": matches}


# --------------------------------------------------------------------------- #
# kg_find_by_path
# --------------------------------------------------------------------------- #
def kg_find_by_path(
    graph: CodeGraph, path: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> dict[str, Any]:
    """Reverse lookup: which node(s) own a source file.

    The inverse of every other tool here — you have a file open and want its
    place in the map. An indexed lookup on `anchor.path`, so it stays cheap on
    a large graph. Matches a bare filename as a path suffix.
    """
    found, paging = _page(
        graph.by_path(path), limit, offset, "Pass a longer repo-relative path"
    )
    # One query for every match's inbound edges, rather than one per match —
    # a bare filename can legitimately hit many nodes.
    inbound = graph.inbound_many([n.id for n, _ in found])
    return {
        "path": path,
        "count": len(found),
        **paging,
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
# kg_find_by_link
# --------------------------------------------------------------------------- #
def kg_find_by_link(graph: CodeGraph, target: str) -> dict[str, Any]:
    """Reverse lookup across graphs: which code nodes point at a node elsewhere.

    The mirror of `kg_find_by_path`, one graph further out. A screen id gives
    you the code that presents it, without opening the screen graph — which
    matters because each plugin has to work with the other absent.

    An indexed lookup on `external_link.target`. Accepts a full
    `<db-file>#<node-id>` or a bare peer node id, since the peer's own tools
    hand back the latter.
    """
    ids = graph.nodes_linking_to(target)
    nodes = {n.id: n for n in graph.nodes(ids, with_edges=False)}
    return {
        "target": target,
        "count": len(ids),
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "description": n.description,
                "anchors": [str(a) for a in n.anchors],
                "external_links": [link.as_dict() for link in n.links],
            }
            for n in (nodes[i] for i in ids if i in nodes)
        ],
        # An empty result is a real answer, not a failure: nothing in this
        # codebase claims a relationship to that node.
        "note": "" if ids else "no node in this graph links to that target",
    }


# --------------------------------------------------------------------------- #
# kg_find_by_reference
# --------------------------------------------------------------------------- #
def kg_find_by_reference(
    graph: CodeGraph,
    query: str | None = None,
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Which nodes depend on documentation matching `query`.

    The review an SDK move calls for: every node citing anything under one part
    of the docs, with the file or function each citation narrows to.
    """
    found, paging = _page(
        graph.references(query, kind), limit, offset, "Pass a query or a kind filter"
    )
    nodes = {n.id: n for n in graph.nodes(sorted({i for i, _ in found}), with_edges=False)}
    return {
        "query": query,
        "kind": kind,
        "count": len(found),
        **paging,
        "references": [
            {
                "node": node_id,
                "node_kind": nodes[node_id].kind if node_id in nodes else "",
                **ref.as_dict(),
            }
            for node_id, ref in found
        ],
    }


# --------------------------------------------------------------------------- #
# kg_parity_gaps
# --------------------------------------------------------------------------- #
def kg_parity_gaps(
    graph: CodeGraph, status: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> dict[str, Any]:
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
    page, paging = _page(gaps, limit, offset, "Pass a status filter")
    return {"count": len(page), **paging, "by_status": breakdown, "gaps": page}


# --------------------------------------------------------------------------- #
# kg_stats
# --------------------------------------------------------------------------- #
def repo_staleness(graph: CodeGraph, repo_root: str | None = None) -> dict[str, Any]:
    """Repo-wide staleness: every anchored file, not just the ones in a change set.

    The number that was missing. Both git hooks scope to a change set, which is
    right for per-commit noise and wrong for a backlog — a file that drifts and
    is never re-derived is reported once, in the commit that touched it, and
    never again. One consumer repo carried 47 stale files for months with every
    check passing, because nothing ever asked this question.

    Goes through `staleness.classify` like every other caller, so the count here
    is the same count the hooks print.
    """
    base = _resolve_source_base(graph, repo_root)
    if base is None:
        return staleness.unchecked("no source tree found for the graph's anchors")
    anchored = graph.anchor_paths()
    split = staleness.classify(
        anchored, graph.sources(), staleness.digest_tree(anchored, base)
    )
    return staleness.report(split, graph.node_ids_for_paths(split.stale))


def kg_stats(graph: CodeGraph, repo_root: str | None = None) -> dict[str, Any]:
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
        # The standing drift total, reported where an agent orients first.
        # `kg_validate` returns this same block from the same helper.
        "staleness": repo_staleness(graph, repo_root),
    }


# --------------------------------------------------------------------------- #
# kg_validate  (the advisory drift detector)
# --------------------------------------------------------------------------- #
def _resolve_source_base(graph: CodeGraph, repo_root: str | None) -> Path | None:
    """Find the directory anchor paths are relative to."""
    return resolve_source_base(
        graph.path.parent, graph.meta.root, graph.anchor_paths(), repo_root
    )


@cache
def _word_pattern(segment: str) -> re.Pattern[str]:
    """A compiled `\\bsegment\\b` matcher, memoized.

    `re`'s internal cache holds 512 patterns; a graph with more distinct symbols
    than that recompiles on every anchor. An explicit memo keeps the hot loop of
    `kg_validate` compiling each symbol once.
    """
    return re.compile(r"\b" + re.escape(segment) + r"\b")


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


@dataclass
class AnchorCheck:
    """What checking every anchor against real source turned up."""

    issues: list[dict[str, str]] = field(default_factory=list)
    checked: int = 0  # anchors
    changed: list[dict[str, str]] = field(default_factory=list)
    unhashed: int = 0  # anchored *files* with no recorded baseline
    split: staleness.Staleness = field(
        default_factory=lambda: staleness.Staleness([], [], [])
    )
    stale_nodes: list[str] = field(default_factory=list)


_CHANGED_ISSUE = (
    "source changed since the graph was built — re-read and "
    "confirm the description still fits"
)


class SourceCache:
    """The source tree as one write's validation runs read it.

    `edits.apply` validates the graph before and after a mutation, and the
    source does not change in between, so both runs walk the tree and read and
    hash each anchored file through this cache once.
    """

    def __init__(self) -> None:
        self._walks: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        self._files: dict[Path, tuple[bool, str | None, str | None]] = {}

    def walk(self, base: Path, keep: Iterable[str]) -> list[str]:
        key = (str(base), tuple(sorted(keep)))
        if key not in self._walks:
            self._walks[key] = walk_sources(base, keep=keep)
        return self._walks[key]

    def read(self, fp: Path) -> tuple[bool, str | None, str | None]:
        if fp not in self._files:
            self._files[fp] = _read_source(fp)
        return self._files[fp]


def _read_source(fp: Path) -> tuple[bool, str | None, str | None]:
    """Whether the file exists, its text, and its digest."""
    if not fp.is_file():
        return False, None, None
    try:
        src: str | None = fp.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        src = None
    return True, src, file_sha(fp)


def _check_anchors(graph: CodeGraph, base: Path, cache: SourceCache | None = None) -> AnchorCheck:
    """Every anchor against real source. The strongest staleness signal there is.

    Two different questions, and the difference is the point:

    - *Does the target still exist?* A missing file or symbol is a broken
      pointer — reported as an issue.
    - *Has the file changed since the description was written?* The stored `sha`
      answers that, and it is what catches the case the symbol check cannot: a
      refactor that keeps a class name but guts its behaviour passes "symbol
      found" cleanly while making the description a lie. Reported separately as
      `changed_since_built`, because a changed file is a prompt to re-read, not
      proof that anything is wrong.

    The second question is not answered here. This walk collects a digest per
    file and hands both digest maps to `staleness.classify`, which is the only
    place in the plugin that decides what "changed" means — see staleness.py for
    the drift that made that worth enforcing.

    `all_anchors()` yields in path order, so each file is stat-ed, read and
    hashed once and only the current file's text is held — rather than caching
    the whole source tree in memory to avoid re-reads.
    """
    out = AnchorCheck()
    anchors = graph.all_anchors()
    current: dict[str, str] = {}
    path: str | None = None
    src: str | None = None
    exists = False
    for node_id, anchor in anchors:
        out.checked += 1
        if anchor.path != path:
            path = anchor.path
            fp = base / anchor.path
            exists, src, sha = cache.read(fp) if cache else _read_source(fp)
            if sha is not None:
                current[anchor.path] = sha
        if not exists:
            out.issues.append({"node": node_id, "anchor": str(anchor), "issue": "file not found"})
            continue
        if anchor.symbol and src is not None and not _symbol_in_source(anchor.symbol, src):
            out.issues.append(
                {"node": node_id, "anchor": str(anchor), "issue": "symbol not found in file"}
            )

    out.split = staleness.classify(graph.anchor_paths(), graph.sources(), current)
    out.unhashed = len(out.split.unbaselined)
    stale = set(out.split.stale)
    out.changed = [
        {"node": node_id, "anchor": str(anchor), "issue": _CHANGED_ISSUE}
        for node_id, anchor in anchors
        if anchor.path in stale
    ]
    out.stale_nodes = graph.node_ids_for_paths(out.split.stale)
    return out


def walk_sources(base: Path, keep: Iterable[str] = ()) -> list[str]:
    """Every file under `base`, relative and posix-separated, ignored dirs pruned.

    os.walk with in-place pruning, not rglob: rglob descends into
    node_modules/.git/build in full and only filters afterwards, so the ignored
    99% of a real tree gets walked and sorted before being discarded.

    `keep` are directories the graph's `covers` names outright (see
    `coverage.declared_roots`); they survive the prune. Without that, a repo that
    declares `.githooks/*` could never reach `covered` — the files were removed
    from the walk before anything classified them, so they counted as neither
    covered nor gap, and the declaration silently did nothing.
    """
    keep_set = {k.strip("/").lower() for k in keep}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [
            d
            for d in dirnames
            if d.lower() not in _IGNORE_LOWER or (prefix + d).lower() in keep_set
        ]
        dirnames.sort()
        out += [prefix + name for name in sorted(filenames)]
    return out


def coverage_report(
    graph: CodeGraph, base: Path, limit: int = 50, cache: SourceCache | None = None
) -> CoverageReport:
    """Which files the graph was supposed to cover, and which it missed.

    The declaration lives in the graph (`meta.covers` / `meta.exempt`), so this
    reports a genuine gap rather than the previous "whatever extensions happen
    to be anchored already" rule, under which a file type with no coverage at
    all produced no warning — see coverage.py for why that mattered.
    """
    meta = graph.meta
    anchored = set(graph.anchor_paths())  # already posix-normalized by the store
    keep = declared_roots(meta.covers)
    files = cache.walk(base, keep) if cache else walk_sources(base, keep=keep)
    return classify(files, anchored, meta.covers, meta.exempt, limit)


def kg_validate(
    graph: CodeGraph,
    peer: CodeGraph | None = None,
    repo_root: str | None = None,
    cache: SourceCache | None = None,
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

    external_link_issues = _check_external_links(graph)
    reference_issues = _check_references(graph)

    base = _resolve_source_base(graph, repo_root)
    checks = AnchorCheck()
    cov = CoverageReport()
    if base is not None:
        checks = _check_anchors(graph, base, cache)
        cov = coverage_report(graph, base, cache=cache)

    # `changed_since_built` is deliberately *not* part of `ok`: it means "go
    # look", not "something is broken". Folding it in would make `ok` false for
    # every graph the moment anyone edits a covered file, which is the failure
    # mode the old date-based freshness gate had.
    # Only the *provably broken* external links count against `ok`. An absent
    # peer graph does not: cartographer is an optional install, and failing here
    # would make it mandatory by the back door (GRAPH-LINKS.md §4).
    broken_links = [i for i in external_link_issues if i["severity"] == "error"]
    ok = not (
        counterpart_issues or description_issues or checks.issues or cov.gaps or broken_links
        or reference_issues
    )
    return {
        "ok": ok,
        "advisory": True,
        "external_link_issues": external_link_issues,
        "reference_issues": reference_issues,
        "source_checked": base is not None,
        "source_base": str(base) if base else None,
        "anchors_checked": checks.checked,
        "anchor_issues": checks.issues,
        "changed_since_built": {
            "count": len(checks.changed),
            "anchors": checks.changed[:50],
            "unhashed": checks.unhashed,
        },
        # The same block `kg_stats` returns, from the same helper — anchors
        # counted above, *files* and the nodes that own them counted here. Two
        # tools that both report drift must not report different numbers.
        "staleness": (
            staleness.report(checks.split, checks.stale_nodes)
            if base is not None
            else staleness.unchecked("no source tree found for the graph's anchors")
        ),
        "coverage": cov.to_dict(),
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


ISSUE_LISTS = (
    "anchor_issues",
    "counterpart_issues",
    "description_issues",
    "external_link_issues",
    "reference_issues",
)


def cap_issues(report: dict[str, Any], limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """For the MCP tool only: `edits.py` compares complete reports before and
    after a write, so `kg_validate` itself must keep returning every issue."""
    out = dict(report)
    counts: dict[str, int] = {}
    cut: list[str] = []
    for key in ISSUE_LISTS:
        items = report[key]
        counts[key] = len(items)
        if len(items) > limit:
            out[key] = items[:limit]
            cut.append(key)
    out["issue_counts"] = counts
    out["truncated"] = bool(cut)
    if cut:
        out["hint"] = (
            f"{', '.join(cut)} capped at {limit}; issue_counts has the totals. "
            f"Pass a larger limit (up to {MAX_LIMIT}) for the complete lists."
        )
    return out


#: What each unresolved outcome means, and how hard it counts. The two soft
#: outcomes are soft for the same reason -- we could not check -- and grading
#: them as errors would make an optional peer plugin mandatory.
_LINK_SEVERITY: dict[Resolution, tuple[str, str]] = {
    Resolution.MALFORMED: (
        "error",
        "not '<db-file>#<node-id>', so nothing can follow it",
    ),
    Resolution.DANGLING: (
        "error",
        "names a node the peer graph does not contain",
    ),
    Resolution.PEER_ABSENT: (
        "warning",
        "peer graph not in this repo — supported, the link is simply unverifiable here",
    ),
    Resolution.PEER_UNREADABLE: (
        "warning",
        "peer file is not a graph we can read — unknown, not broken",
    ),
}


def _check_external_links(graph: CodeGraph) -> list[dict[str, str]]:
    """Follow every cross-graph link and report what did not resolve.

    Resolved against the directory the graph sits in, because that is what a
    target is relative to (GRAPH-LINKS.md §2). Unlike `counterpart`, these are
    *not* required to be reciprocal: "this code presents that screen" is not a
    symmetric claim and has no obligation to be mirrored, whereas parity between
    two codebases is and does.
    """
    issues: list[dict[str, str]] = []
    knowledge_dir = graph.path.parent
    for node_id, link in graph.external_links():
        outcome = resolve(knowledge_dir, link.target)
        if outcome is Resolution.OK:
            continue
        severity, why = _LINK_SEVERITY[outcome]
        issues.append(
            {
                "node": node_id,
                "target": link.target,
                "severity": severity,
                "issue": why,
            }
        )
    return issues


def _check_references(graph: CodeGraph) -> list[dict[str, str]]:
    """References whose `path` / `symbol` is not one of their node's own anchors.

    The writer refuses these, so a finding here means the row arrived another
    way: hand-written SQL, or a tool that replaced a node's anchors without
    going through `clean.node_problems`. Unchecked, the two columns are free
    text. Matched against anchors rather than against source because anchors
    are what `_check_anchors` already resolves; a narrowing that equals an
    anchor inherits that check.
    """
    narrowed = [(i, r) for i, r in graph.references() if r.path or r.symbol]
    if not narrowed:
        return []
    anchors = {
        n.id: n.anchors
        for n in graph.nodes(sorted({i for i, _ in narrowed}), with_edges=False)
    }
    issues: list[dict[str, str]] = []
    for node_id, ref in narrowed:
        problem = ref.problem(anchors.get(node_id, []))
        if problem:
            issues.append(
                {"node": node_id, "url": ref.url, "narrows_to": ref.narrowing, "issue": problem}
            )
    return issues


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
    "Anchor",
    "cap_issues",
    "coverage_report",
    "kg_find_by_kind",
    "kg_find_by_link",
    "kg_find_by_path",
    "kg_find_by_reference",
    "kg_neighborhood",
    "kg_node",
    "kg_parity_gaps",
    "kg_search",
    "kg_stats",
    "kg_validate",
    "open_peer",
    "repo_staleness",
    "walk_sources",
]

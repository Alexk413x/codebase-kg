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

import json
import os
import re
from importlib import metadata
from urllib.parse import urlparse
from urllib.request import url2pathname
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clean, staleness
from .coverage import CoverageReport, classify, declared_roots, resolve_source_base
from .links import Resolution, resolve
from .models import Anchor, Node
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
    graph: CodeGraph, query: str | None = None, kind: str | None = None
) -> dict[str, Any]:
    """Which nodes depend on documentation matching `query`.

    The review an SDK move calls for: every node citing anything under one part
    of the docs, with the file or function each citation narrows to.
    """
    found = graph.references(query, kind)
    nodes = {n.id: n for n in graph.nodes(sorted({i for i, _ in found}), with_edges=False)}
    return {
        "query": query,
        "kind": kind,
        "count": len(found),
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
def _installed_from() -> Path | None:
    """The directory this package was installed from, per its own dist metadata.

    A local install records its source in `direct_url.json` (PEP 610), which is
    what `uvx --from <plugin>/mcp` produces. Nothing else survives that install:
    the package lands in a venv's site-packages, so walking up from `__file__`
    finds a lib directory, not the plugin.
    """
    try:
        raw = metadata.distribution("codebase-kg").read_text("direct_url.json")
        url = json.loads(raw or "").get("url", "")
    except (metadata.PackageNotFoundError, OSError, ValueError, AttributeError):
        return None
    if not url.startswith("file:"):
        return None  # installed from an index or a VCS — no local path to hand back
    path = Path(url2pathname(urlparse(url).path))
    return path if (path / "pyproject.toml").is_file() else None


def _checkout_root() -> Path | None:
    """The repo this package is being developed in, if that is where it lives."""
    checkout = Path(__file__).resolve().parents[2]
    return checkout if (checkout / "pyproject.toml").is_file() else None


def package_root() -> Path | None:
    """Where the CLIs can be run from, or None if only the console scripts can.

    Three ways this package is reached, in order of how directly they answer:
    the repo checkout it is being developed in, the directory it was installed
    from, and the plugin root the host exported.
    """
    checkout = _checkout_root()
    if checkout is not None:
        return checkout
    installed = _installed_from()
    if installed is not None:
        return installed
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        candidate = Path(env) / "mcp"
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def cli_invocations() -> dict[str, str]:
    """Ready-to-run commands for the build / export / migrate CLIs.

    The skills used to instruct `python -m codebase_kg.build`, which works only
    where the package is importable — not in a target repo that has the plugin
    but no install, where it is a bare `ModuleNotFoundError`. A skill cannot
    assemble the `uvx --from` form for itself either: `CLAUDE_PLUGIN_ROOT` is not
    set in the shell a skill's Bash runs in, and the console scripts are not on
    its PATH. The server knows where it was loaded from, so it answers rather
    than the caller guessing.

    Resolving only against the source checkout was the first attempt and was
    wrong in the one configuration that ships: under `uvx --from`, `__file__` is
    inside a venv, so it fell through to the bare console script — a command the
    caller cannot run. `package_root` covers the installed case too.
    """
    root = package_root()
    prefix = f'uvx --from "{root}" ' if root is not None else ""
    return {
        "package_root": str(root) if root is not None else "",
        "build": f"{prefix}codebase-kg-build",
        "export": f"{prefix}codebase-kg-export",
        "migrate": f"{prefix}codebase-kg-migrate",
    }


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
        # How to run the CLIs from THIS repo. Skills read this instead of
        # hardcoding an invocation that only works inside the plugin's checkout.
        "cli": cli_invocations(),
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


def _check_anchors(graph: CodeGraph, base: Path) -> AnchorCheck:
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
            path, src = anchor.path, None
            fp = base / anchor.path
            exists = fp.is_file()
            if exists:
                try:
                    src = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    src = None
                sha = file_sha(fp)
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


def coverage_report(graph: CodeGraph, base: Path, limit: int = 50) -> CoverageReport:
    """Which files the graph was supposed to cover, and which it missed.

    The declaration lives in the graph (`meta.covers` / `meta.exempt`), so this
    reports a genuine gap rather than the previous "whatever extensions happen
    to be anchored already" rule, under which a file type with no coverage at
    all produced no warning — see coverage.py for why that mattered.
    """
    meta = graph.meta
    anchored = set(graph.anchor_paths())  # already posix-normalized by the store
    files = walk_sources(base, keep=declared_roots(meta.covers))
    return classify(files, anchored, meta.covers, meta.exempt, limit)


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

    external_link_issues = _check_external_links(graph)
    reference_issues = _check_references(graph)

    base = _resolve_source_base(graph, repo_root)
    checks = AnchorCheck()
    cov = CoverageReport()
    if base is not None:
        checks = _check_anchors(graph, base)
        cov = coverage_report(graph, base)

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
    "open_peer",
    "kg_search",
    "kg_node",
    "kg_neighborhood",
    "kg_find_by_kind",
    "kg_find_by_path",
    "kg_find_by_link",
    "kg_find_by_reference",
    "kg_parity_gaps",
    "kg_stats",
    "kg_validate",
    "repo_staleness",
    "coverage_report",
    "walk_sources",
    "Anchor",
]

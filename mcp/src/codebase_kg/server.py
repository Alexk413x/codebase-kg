"""FastMCP server entry point for codebase-kg.

Sixteen tools over one repo's `knowledge/code_graph.db` — ten queries and six
targeted writes. The graph opens on each tool call, so tools always see current
data — including a graph created after the server started. The peer graph named
in `meta.counterpart` is opened the same way for the cross-codebase parity
checks.

The write tools are for **targeted** changes: one node's description, an anchor
that moved, a cross-graph link. Bulk work — a parity sweep, a restructuring,
anything where reviewing the diff before applying it is the point — still goes
through `export → edit the JSON → build`. Each tool's description says which it
is, because picking the wrong one is the way this surface gets misused.

Path resolution order: CLI arg → $CODEBASE_KG_PATH → walk up from CWD honoring
an optional `graph_path` in `.claude/codebase-kg.local.md`, else
`knowledge/code_graph.db` (no repo-root fallback). While unresolved, the path is
re-resolved on every tool call so a freshly built graph is picked up.

Under `--serve` (see `daemon.py`) one process serves every session on the
machine, so its own argv, environment and cwd describe none of them. Each
connection's handshake carries the session's cwd and explicit graph path, and
`bind_connection` puts them in a context variable that every tool call in that
connection resolves from, in the same order. The resolved path is cached per
connection, never process-wide.

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
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Iterator

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import ConfigDict, Field, SkipValidation, with_config
from typing_extensions import Required, TypedDict

from . import cli as _cli
from . import edits as _edits
from . import tools as _tools
from .store import CodeGraph, StoreError

GRAPH_FILENAME = "code_graph.db"
# The pre-rewrite artifact. Only ever used to produce a better error message.
LEGACY_FILENAME = "KNOWLEDGE_GRAPH.md"

INSTRUCTIONS = (
    "codebase-kg serves a committed map of this repository's code in "
    "knowledge/code_graph.db: components, the files and symbols that implement them, "
    "and how they depend on each other. The tools apply only in a repo that has that "
    "file; elsewhere they return an error. kg_search is the entry point: it finds "
    "components by concept, including ones whose names lack the search words. kg_node "
    "and kg_neighborhood give anchors and dependents, and kg_find_by_path maps a file "
    "back to its component. In a repo with a graph, a hook denies a Grep or Glob over "
    "mapped source until a graph query has been made."
)

mcp: FastMCP[Any] = FastMCP("codebase-kg", instructions=INSTRUCTIONS)

_QUERY: dict[str, Any] = {"readOnlyHint": True, "openWorldHint": False}
_WRITE: dict[str, Any] = {"readOnlyHint": False, "openWorldHint": False}
_DESTRUCTIVE: dict[str, Any] = {**_WRITE, "destructiveHint": True}
# Passed explicitly to the write tools: a `ToolResult` in the return type stops
# fastmcp inferring any output schema, so the writes would otherwise lose it.
_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": True}

Limit = Annotated[
    int,
    Field(
        ge=1,
        le=_tools.MAX_LIMIT,
        description=(
            "Most items to return. The result's `total` counts every match and "
            "`truncated` says whether more remain."
        ),
    ),
]
Offset = Annotated[
    int,
    Field(ge=0, description="Items to skip, for the next page: the previous result's `next_offset`."),
]

# Only the resolved *path* is remembered between calls — never an open handle.
_graph_path: Path | None = None


@dataclass
class Connection:
    """One shared-server session: where it runs, and the graph it resolved."""

    cwd: Path
    explicit: Path | None = None
    resolved: Path | None = None


_connection: ContextVar[Connection | None] = ContextVar("codebase_kg_connection", default=None)
_serving = False


def enter_serve_mode() -> None:
    global _serving
    _serving = True


def bind_connection(conn: Connection) -> Token[Connection | None]:
    return _connection.set(conn)


def _current() -> Connection | None:
    conn = _connection.get()
    if conn is None and _serving:
        # Never fall back to Path.cwd(), argv or the environment in serve mode:
        # they belong to whichever session happened to start the server.
        raise RuntimeError("tool call outside a connection context in serve mode")
    return conn


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


def _search_start() -> Path:
    conn = _current()
    return conn.cwd if conn is not None else Path.cwd()


def _resolve_graph_path() -> Path | None:
    conn = _current()
    if conn is not None:
        if conn.explicit is not None:
            return conn.explicit.resolve()
    elif len(sys.argv) > 1 and sys.argv[1].strip():
        return Path(sys.argv[1]).resolve()
    elif os.environ.get("CODEBASE_KG_PATH"):
        return Path(os.environ["CODEBASE_KG_PATH"]).resolve()
    cwd = _search_start()
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
    cwd = _search_start()
    for base in (cwd, *cwd.parents):
        cand = base / "knowledge" / LEGACY_FILENAME
        if cand.is_file():
            return cand
    return None


def _missing_graph_error() -> FileNotFoundError:
    legacy = _find_legacy()
    if legacy is not None:
        migrate = _cli.command("migrate", f'"{legacy}"')
        return FileNotFoundError(
            f"Found a pre-rewrite {LEGACY_FILENAME} at {legacy} but no {GRAPH_FILENAME}. "
            f"Migrate it once with:\n"
            f"    {migrate}\n"
            f"then commit knowledge/{GRAPH_FILENAME}."
        )
    return FileNotFoundError(
        f"No {GRAPH_FILENAME} found. Run /codebase-kg:build to create one, pass its "
        f"path as the first CLI arg, or set $CODEBASE_KG_PATH."
    )


def _graph_file() -> Path:
    global _graph_path
    conn = _current()
    path = conn.resolved if conn is not None else _graph_path
    if path is None or not path.is_file():
        # Re-resolve while unresolved (or if the file went away) — the graph may
        # have been created after the server started (e.g. /codebase-kg:build).
        path = _resolve_graph_path()
        if conn is not None:
            conn.resolved = path
        else:
            _graph_path = path
    if path is None or not path.is_file():
        raise _missing_graph_error()
    return path


@contextmanager
def _open_graph() -> Iterator[CodeGraph]:
    """Open the graph for one tool call, then close it."""
    g = CodeGraph(_graph_file())
    try:
        yield g
    finally:
        g.close()


def _write(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any] | ToolResult:
    """Run one write tool, turning a refusal into an answer rather than a crash.

    A rejected edit is a normal outcome — it is the tool working — so it comes
    back as data an agent can act on, flagged `isError` so the client sees a
    failure. `written: false` is the load-bearing field: it is how the caller
    knows the committed artifact is byte-identical to before.
    """
    try:
        return fn(_graph_file(), *args, **kwargs)
    except (_edits.EditError, StoreError, FileNotFoundError) as exc:
        refusal = {"ok": False, "written": False, "error": str(exc)}
        return ToolResult(structured_content=refusal, is_error=True)


# An optional parameter declares its default inside `Field`, not as `= None`:
# fastmcp wraps a `= None` parameter in a second anyOf and its description ends up nested inside it.
@mcp.tool(annotations=_QUERY)
def kg_search(
    query: Annotated[
        str, Field(description='Free-form text, e.g. "feed ranking" or "bookmark persistence".')
    ],
    kind: Annotated[
        str | None,
        Field(default=None, description='Optional case-insensitive substring of the node\'s `kind`, e.g. "ViewModel".'),
    ],
) -> dict[str, Any]:
    """Full-text search the code graph for nodes matching a phrase. Use when you
    have a concept but no node id — this is the usual entry point. Ranked, up to
    10 results, each with its `path#Symbol` anchors so you can go straight to the
    code. CamelCase identifiers are matched in split form, so "video playback"
    finds `VideoPlaybackService`."""
    with _open_graph() as g:
        return _tools.kg_search(g, query, kind)


@mcp.tool(annotations=_QUERY)
def kg_node(
    id: Annotated[
        str, Field(description="Exact node id, as returned by `kg_search` or another tool.")
    ],
) -> dict[str, Any]:
    """Fetch one full node by id — anchors (`path#Symbol`), description, edges,
    parity, counterpart, external links and references, plus inbound edges. The
    primary lookup once you know the id. Returns `found: false` with
    `did_you_mean` suggestions when the id is unknown.

    Not for finding a node by concept; use `kg_search`. It does not return the
    neighbours' details or any source text; use `kg_neighborhood` for the nodes
    around it."""
    with _open_graph() as g:
        return _tools.kg_node(g, id)


@mcp.tool(annotations=_QUERY)
def kg_neighborhood(
    id: Annotated[str, Field(description="Exact id of the node at the center.")],
    depth: Annotated[
        int,
        Field(description="Hops to expand, 1 to 3; other values are clamped. Depth 3 can reach most of a graph."),
    ] = 1,
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Return a node with its graph neighborhood — outbound edges, inbound edges
    (who depends on it), and counterpart — expanded `depth` hops. Use to
    understand what surrounds a node before changing it. Neighbors come nearest
    first and are paged by `limit`; a truncated result says how to narrow."""
    with _open_graph() as g:
        return _tools.kg_neighborhood(g, id, depth, limit, offset)


@mcp.tool(annotations=_QUERY)
def kg_find_by_kind(
    kind: Annotated[
        str,
        Field(description='Case-insensitive substring of the node\'s `kind`, e.g. "viewmodel". An empty string matches every node.'),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List every node whose free-text `kind` matches (case-insensitive
    substring) — e.g. all `ViewModel`s, `Service`s, `Room @Entity`s. Use for a
    category question such as "every migration".

    Not for a concept or a name; use `kg_search`, because `kind` is free text
    and one category can be spelled several ways. Returns id, kind, description
    and anchors per node, not edges; call `kg_node` for those."""
    with _open_graph() as g:
        return _tools.kg_find_by_kind(g, kind, limit, offset)


@mcp.tool(annotations=_QUERY)
def kg_find_by_path(
    path: Annotated[
        str,
        Field(description="A repo-relative path, or a bare filename matched as a path suffix, e.g. `FeedRanker.kt`."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Reverse lookup: given a source file, which node(s) own it, and what do
    they connect to. Use when you have a file open and want its place in the map.
    Accepts a repo-relative path or a bare filename (matched as a suffix)."""
    with _open_graph() as g:
        return _tools.kg_find_by_path(g, path, limit, offset)


@mcp.tool(annotations=_QUERY)
def kg_find_by_link(
    target: Annotated[
        str,
        Field(description="A full `<db-file>#<node-id>` target, or a bare peer node id."),
    ],
) -> dict[str, Any]:
    """Reverse lookup across graphs: which code node(s) link to a node in another
    committed graph in this repo — typically a screen in `cartographer/baselines/baseline.db`.

    Use when you have a screen and want the code behind it. Accepts a full
    `<db-file>#<node-id>` target or a bare peer node id. See cartographer's
    docs/GRAPH-LINKS.md for the convention."""
    with _open_graph() as g:
        return _tools.kg_find_by_link(g, target)


@mcp.tool(annotations=_QUERY)
def kg_find_by_reference(
    query: Annotated[
        str | None,
        Field(default=None, description="Case-insensitive substring of the reference url or title, e.g. `developer.android.com/reference/android/view`. Omit to list every reference."),
    ],
    kind: Annotated[
        str | None,
        Field(default=None, description="Exact tag filter: `platform-api`, `spec`, `rfc` or `issue`."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Which node(s) depend on a piece of external documentation. Use when an SDK
    or spec moves and you need every place the code relies on it.

    Each hit carries the `path` / `symbol` it narrows to, when it has one."""
    with _open_graph() as g:
        return _tools.kg_find_by_reference(g, query, kind, limit, offset)


@mcp.tool(annotations=_QUERY)
def kg_parity_gaps(
    status: Annotated[
        str | None,
        Field(default=None, description="Optional filter: 'divergent', 'only' (any *-only), or an exact flag like 'android-only'."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """The cross-codebase gap report, as a query. Lists nodes flagged
    `divergent` or `<codebase>-only`, with their counterpart + divergence line.
    `by_status` counts every gap, including any past `limit`."""
    with _open_graph() as g:
        return _tools.kg_parity_gaps(g, status, limit, offset)


@mcp.tool(annotations=_QUERY)
def kg_stats() -> dict[str, Any]:
    """Counts and health for cold start: node/edge/anchor totals, breakdown by
    kind and section, parity breakdown, isolated nodes, when the graph was
    generated, and `staleness` — the repo-wide count of mapped files whose
    contents no longer match what the graph was built against, plus the nodes
    that describe them. Read `staleness.stale_files` before trusting a
    description: non-zero means part of this map is out of date, and
    `/codebase-kg:audit` says which part."""
    with _open_graph() as g:
        return _tools.kg_stats(g)


@mcp.tool(annotations=_QUERY)
def kg_validate(
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=_tools.MAX_LIMIT,
            description="Most entries per issue list. `issue_counts` has each list's full length.",
        ),
    ] = _tools.DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Advisory drift check against real source (never blocks). Reports anchors
    whose file or symbol no longer exists, source files under the root that no
    node covers, and counterpart problems vs the peer graph. Structural
    integrity — unique ids, no dangling edges, consistent parity — is guaranteed
    by the store and reported rather than checked. Each issue list is capped at
    `limit`, and `changed_since_built` and `coverage.gaps` at 50."""
    with _open_graph() as g, _tools.open_peer(g) as peer:
        return _tools.cap_issues(_tools.kg_validate(g, peer), limit)


class LinkSpec(TypedDict, total=False):
    target: Required[
        Annotated[str, Field(description="`<peer-db>#<node>`, relative to `knowledge/`.")]
    ]
    kind: Annotated[
        str, Field(description="What the link means from this side, e.g. `presented-by`.")
    ]


class ReferenceSpec(TypedDict, total=False):
    url: Required[
        Annotated[str, Field(description="The documentation page, e.g. `https://...`.")]
    ]
    kind: Annotated[str, Field(description="`platform-api`, `spec`, `rfc` or `issue`.")]
    title: Annotated[str, Field(description="A human label, e.g. `TouchDelegate`.")]
    path: Annotated[
        str, Field(description="Optional. Must equal the path of one of this node's anchors.")
    ]
    symbol: Annotated[
        str, Field(description="Optional. With `path`, must equal one of this node's anchors.")
    ]


# Every field skips validation and extra keys are kept: `edits.upsert_node` does
# its own checks and returns them as refusals, and it tolerates keys it ignores.
@with_config(ConfigDict(extra="allow"))
class NodePatch(TypedDict, total=False):
    id: Required[
        Annotated[
            SkipValidation[str],
            Field(description="Node id. A node that does not exist yet also needs `kind`."),
        ]
    ]
    kind: Annotated[
        SkipValidation[str],
        Field(description="Free text, e.g. 'ViewModel', 'Service', 'module'."),
    ]
    description: Annotated[
        SkipValidation[str],
        Field(description="What the node does. Refused if it fails the description checks, e.g. a ticket ref."),
    ]
    section: Annotated[
        SkipValidation[str], Field(description="The section the node is listed under.")
    ]
    parity: Annotated[
        SkipValidation[str | None],
        Field(description="`matched`, `divergent` or `<codebase>-only`. `null` clears it."),
    ]
    counterpart: Annotated[
        SkipValidation[str | None],
        Field(description="`<peer-graph-relative-path>#<peer-node-id>`. `null` clears it."),
    ]
    divergence: Annotated[
        SkipValidation[str | None],
        Field(description="One line, only with parity `divergent`. `null` clears it."),
    ]
    anchors: Annotated[
        SkipValidation[list[str]],
        Field(description='`["path/to/File.kt#Symbol", ...]` — symbols, never line numbers. Replaces the whole list.'),
    ]
    edges: Annotated[
        SkipValidation[list[str]],
        Field(description="Outbound node ids. Both endpoints must exist after this call. Replaces the whole list."),
    ]
    external_links: Annotated[
        SkipValidation[list[LinkSpec]],
        Field(description="Links to nodes in other committed graphs. Replaces the whole list."),
    ]
    references: Annotated[
        SkipValidation[list[ReferenceSpec]],
        Field(description="Where the facts this node depends on are documented. Replaces the whole list."),
    ]
    rebaseline: Annotated[
        SkipValidation[bool],
        Field(
            description=(
                "`true` records that you checked this node against its source now. It "
                "re-hashes every file the node anchors, so those files leave "
                "`changed_since_built` and the pre-push stale-file list. The baseline belongs to "
                "the file, not the node: it clears every node anchored to that file, the "
                "same as `build --rebaseline`. Set it only after you have checked every "
                "node that anchors those files. Without it, a recorded baseline never "
                "changes, so a description edit alone cannot hide drift."
            )
        ),
    ]


@mcp.tool(annotations={**_WRITE, "destructiveHint": False, "idempotentHint": True}, output_schema=_OBJECT)
def kg_upsert_node(
    nodes: Annotated[
        list[NodePatch],
        Field(description="One object per node. Only the keys present change."),
    ],
) -> dict[str, Any] | ToolResult:
    """Atomic: if any node in the batch is rejected, nothing is written and the
    file is byte-identical, and the result is an error with `ok: false`,
    `written: false` and the reason.

    Create or update node(s) in place — the targeted alternative to
    export/edit/build. Use for a handful of nodes: a wrong description, an anchor
    that moved, a missing edge. For bulk work (a parity sweep, a restructuring,
    anything you want to review as a diff first) still use the plugin's
    `mcp/launch/kg_cli.py export` → edit the JSON → `kg_cli.py build`.

    **Only the keys you supply change** — omit a field and it keeps its value,
    pass `null` to clear `parity`/`counterpart`/`divergence`. `anchors`, `edges`,
    `external_links` and `references` replace the whole list when present, so read
    the node first if you mean to append. Baselines follow the anchors as a
    build's do: a file this node anchors that has no baseline gets one, and a
    file no node anchors any more loses its own.

    Returns every row and field it changed, before and after, including each
    `source` row (file baseline) it created, re-hashed or dropped.
    """
    return _write(_edits.upsert_node, nodes)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_delete_node(
    ids: Annotated[list[str], Field(description="Ids of the nodes to delete.")],
    dry_run: Annotated[
        bool, Field(description="True previews what would go and writes nothing. Pass false to apply.")
    ] = True,
    cascade_inbound: Annotated[
        bool,
        Field(description="Also remove edges from other nodes that point at these, in the same atomic call. Without it such edges block the delete."),
    ] = False,
) -> dict[str, Any] | ToolResult:
    """Delete node(s). **Previews by default** — call with `dry_run=false` to apply.

    A node does not leave alone: its anchors, its outbound edges and its external
    links cascade away with it, and so does the baseline of any file no remaining
    node anchors. Edges pointing *at* it do not — `ON DELETE
    RESTRICT` blocks the delete instead, so a relationship is never dropped by
    accident. Pass `cascade_inbound=true` to remove those edges as part of the
    same atomic call, having seen them in the dry run.

    The preview lists exactly what would go. Nothing is written unless
    `dry_run=false`, and even then the delete is rejected whole if it would break
    the graph."""
    return _write(_edits.delete_node, ids, dry_run=dry_run, cascade_inbound=cascade_inbound)


@mcp.tool(annotations=_WRITE, output_schema=_OBJECT)
def kg_add_link(
    node_id: Annotated[str, Field(description="Id of the code node the link starts from.")],
    target: Annotated[
        str,
        Field(description="`<db-file>#<node-id>`, relative to `knowledge/` and never absolute (an absolute path breaks on the next clone)."),
    ],
    kind: Annotated[
        str,
        Field(description="What the link means from this side: `implements`, `presented-by`, `tests`, `documents`. Empty means unspecified."),
    ] = "",
) -> dict[str, Any] | ToolResult:
    """Point a code node at a node in another committed graph in this repo —
    typically a screen in `cartographer/baselines/baseline.db`. The inverse of `kg_find_by_link`.
    See cartographer's docs/GRAPH-LINKS.md for the target convention. An empty
    `kind` is honest for a link nobody has characterised.

    Refused if the peer graph is present and does not contain that node, so a
    typo'd target cannot be committed."""
    return _write(_edits.add_link, node_id, target, kind)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_remove_link(
    node_id: Annotated[str, Field(description="Id of the code node that holds the link.")],
    target: Annotated[
        str, Field(description="The link's `<db-file>#<node-id>` target, as `kg_node` lists it.")
    ],
) -> dict[str, Any] | ToolResult:
    """Remove one cross-graph link from a node. The node, its anchors and its
    edges are untouched — this drops the pointer only. Refused when the node has
    no link to `target`.

    Not for a documentation reference (use `kg_remove_reference`) or an edge
    between code nodes (use `kg_upsert_node` with the new `edges` list). Returns
    the change, not the node; call `kg_node` to see the links that remain."""
    return _write(_edits.remove_link, node_id, target)


@mcp.tool(annotations=_WRITE, output_schema=_OBJECT)
def kg_add_reference(
    node_id: Annotated[str, Field(description="Id of the code node that depends on the documented fact.")],
    url: Annotated[str, Field(description="The documentation page's URL.")],
    kind: Annotated[
        str, Field(default="", description="A short tag: `platform-api`, `spec`, `rfc`, `issue`.")
    ],
    title: Annotated[str, Field(default="", description="A human label for the page.")],
    path: Annotated[
        str | None,
        Field(default=None, description="Optional. Narrows the reference to one file; must equal the path of one of the node's anchors."),
    ],
    symbol: Annotated[
        str | None,
        Field(default=None, description="Optional. With `path`, narrows to one function; `path` + `symbol` must equal one of the node's anchors."),
    ],
) -> dict[str, Any] | ToolResult:
    """Record where a fact a code node depends on is documented — a platform API
    page, a spec, an RFC, an issue. The inverse of `kg_find_by_reference`.

    A `path` / `symbol` narrowing that matches none of the node's own anchors is
    refused — read the node first.

    Not for a link to a node in another graph; that is `kg_add_link`."""
    return _write(_edits.add_reference, node_id, url, kind, title, path, symbol)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_remove_reference(
    node_id: Annotated[str, Field(description="Id of the code node that holds the reference.")],
    url: Annotated[str, Field(description="The reference's URL, exactly as `kg_node` lists it.")],
    path: Annotated[
        str | None,
        Field(default=None, description="Optional. The narrowing's path; with it, only the reference with exactly this `path` and `symbol` goes."),
    ],
    symbol: Annotated[
        str | None,
        Field(default=None, description="Optional. The narrowing's symbol; with it, only the reference with exactly this `path` and `symbol` goes."),
    ],
) -> dict[str, Any] | ToolResult:
    """Remove a node's reference(s) to `url`. With `path` / `symbol`, removes only
    the reference with that exact narrowing; without, every reference to the url.
    Refused when nothing matches.

    Not for a link to a node in another graph; that is `kg_remove_link`. Returns
    the change, not the node; call `kg_node` to see the references that remain."""
    return _write(_edits.remove_reference, node_id, url, path, symbol)


def main() -> None:
    # Resolve nothing here: the server must start cleanly in a repo that has no
    # graph yet (e.g. before /codebase-kg:build). A missing graph surfaces as an
    # actionable error on first use, not as a server that refuses to start.
    if sys.argv[1:2] == ["--serve"]:
        from .daemon import serve

        serve()
        return
    mcp.run()


if __name__ == "__main__":
    main()

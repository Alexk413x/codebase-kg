"""The tool definitions: sixteen fastmcp registrations, the source of `catalog.json`.

Nothing at runtime imports this module. The shared server (`daemon.py`) serves
the catalog generated from these registrations by `mcp/scripts/gen_catalog.py`,
and checks arguments against it in `core.py`, so it never loads fastmcp,
pydantic or the `mcp` package. A test fails when `catalog()` and the committed
`catalog.json` differ: after changing a tool's signature, description or
annotations here, regenerate the catalog.

The tool bodies also run, in this process, for the tests that compare fastmcp's
handling of a call with the core's. They resolve the graph the way a private
server does, from the first CLI arg, `$CODEBASE_KG_PATH` or the cwd (see
`resolve.py`).

The write tools are for **targeted** changes: one node's description, an anchor
that moved, a cross-graph link. Bulk work — a parity sweep, a restructuring,
anything where reviewing the diff before applying it is the point — still goes
through `export → edit the JSON → build`. Each tool's description says which it
is, because picking the wrong one is the way this surface gets misused.

**The graph is opened per tool call and closed again.** That is affordable
precisely because opening a store is constant-time (~1 ms) rather than a parse
whose cost grows with the graph — there is nothing to amortize. It also matters
for correctness: a held-open handle blocks Windows from replacing the file, so
caching the connection would make `/codebase-kg:refresh` fail to write its own
output whenever the server was running. Not holding the file means every call
sees current data with no cache-invalidation logic at all.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from pydantic import ConfigDict, Field, SkipValidation, with_config
from typing_extensions import Required, TypedDict

from . import edits as _edits
from . import query as _query
from . import resolve
from . import tools as _tools
from . import worker as _worker
from .store import CodeGraph, StoreError

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


def _graph_file() -> Path:
    global _graph_path
    conn = resolve.from_process(sys.argv)
    conn.resolved = _graph_path
    _graph_path = resolve.graph_file(conn)
    return _graph_path


def _read(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    g = CodeGraph(_graph_file())
    try:
        return _query.TOOLS[tool](g, args)
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
        return ToolResult(structured_content=_worker.refusal(str(exc)), is_error=True)


def catalog() -> dict[str, Any]:
    """The `tools/list` result as a client receives it, without fastmcp's own `_meta`, and the instructions."""

    async def listed() -> list[dict[str, Any]]:
        async with Client(mcp) as client:
            result = await client.list_tools_mcp()
        return result.model_dump(by_alias=True, exclude_none=True, mode="json")["tools"]

    tools = asyncio.run(listed())
    for tool in tools:
        meta = tool.get("_meta", {})
        meta.pop("fastmcp", None)
        if not meta:
            tool.pop("_meta", None)
    return {"instructions": INSTRUCTIONS, "tools": tools}


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
    return _read("kg_search", {"query": query, "kind": kind})


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
    return _read("kg_node", {"id": id})


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
    return _read("kg_neighborhood", {"id": id, "depth": depth, "limit": limit, "offset": offset})


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
    return _read("kg_find_by_kind", {"kind": kind, "limit": limit, "offset": offset})


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
    return _read("kg_find_by_path", {"path": path, "limit": limit, "offset": offset})


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
    return _read("kg_find_by_link", {"target": target})


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
    return _read("kg_find_by_reference", {"query": query, "kind": kind, "limit": limit, "offset": offset})


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
    return _read("kg_parity_gaps", {"status": status, "limit": limit, "offset": offset})


@mcp.tool(annotations=_QUERY)
def kg_stats() -> dict[str, Any]:
    """Counts and health for cold start: node/edge/anchor totals, breakdown by
    kind and section, parity breakdown, isolated nodes, when the graph was
    generated, and `staleness` — the repo-wide count of mapped files whose
    contents no longer match what the graph was built against, plus the nodes
    that describe them. Read `staleness.stale_files` before trusting a
    description: non-zero means part of this map is out of date, and
    `/codebase-kg:audit` says which part."""
    return _read("kg_stats", {})


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
    return _read("kg_validate", {"limit": limit})


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

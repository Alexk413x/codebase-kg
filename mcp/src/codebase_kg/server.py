"""The tool definitions: thirteen fastmcp registrations, the source of `catalog.json`.

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
            "Page size."
        ),
    ),
]
Offset = Annotated[
    int,
    Field(ge=0, description="The previous result's `next_offset`."),
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
    query: Annotated[str, Field(description='Free text, e.g. "feed ranking".')],
    kind: Annotated[
        str | None,
        Field(default=None, description="Case-insensitive substring of the node's `kind`."),
    ],
) -> dict[str, Any]:
    """Full-text search for nodes by concept when you have no node id; the usual
    entry point. Up to 10 ranked results with `path#Symbol` anchors. CamelCase
    names match in split form."""
    return _read("kg_search", {"query": query, "kind": kind})


@mcp.tool(annotations=_QUERY)
def kg_node(
    id: Annotated[str, Field(description="Node id.")],
) -> dict[str, Any]:
    """Fetch one node by id, with anchors, description, edges, inbound edges,
    links and references. An unknown id returns `found: false` and `did_you_mean`.
    No source text or neighbour details (use `kg_neighborhood`)."""
    return _read("kg_node", {"id": id})


@mcp.tool(annotations=_QUERY)
def kg_neighborhood(
    id: Annotated[str, Field(description="Center node id.")],
    depth: Annotated[
        int,
        Field(description="Hops, 1 to 3 (others clamped). Depth 3 can reach most of a graph."),
    ] = 1,
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """A node with its outbound edges, inbound edges (who depends on it) and
    counterpart, expanded `depth` hops. Use to see what surrounds a node before
    changing it. Nearest first, paged by `limit`."""
    return _read("kg_neighborhood", {"id": id, "depth": depth, "limit": limit, "offset": offset})


@mcp.tool(annotations=_QUERY)
def kg_find_by_kind(
    kind: Annotated[
        str,
        Field(description="Case-insensitive substring of `kind`. Empty matches every node."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List every node whose free-text `kind` matches, for a category question such
    as "every migration". For a concept or name use `kg_search`: one category can
    be spelled several ways. Returns id, kind, description and anchors, not edges."""
    return _read("kg_find_by_kind", {"kind": kind, "limit": limit, "offset": offset})


@mcp.tool(annotations=_QUERY)
def kg_find_by_path(
    path: Annotated[
        str,
        Field(description="Repo-relative path, or a bare filename matched as a suffix, e.g. `FeedRanker.kt`."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Which node(s) own a source file, and what they connect to. Use when you
    have a file and want its place in the map."""
    return _read("kg_find_by_path", {"path": path, "limit": limit, "offset": offset})


@mcp.tool(annotations=_QUERY)
def kg_find_by_link(
    target: Annotated[
        str,
        Field(description="A `<db-file>#<node-id>` target, or a bare peer node id."),
    ],
) -> dict[str, Any]:
    """Which code node(s) link to a node in another committed graph in this repo,
    typically a screen in `baselines/baseline.db`. Use when you have a screen
    and want the code behind it."""
    return _read("kg_find_by_link", {"target": target})


@mcp.tool(annotations=_QUERY)
def kg_find_by_reference(
    query: Annotated[
        str | None,
        Field(default=None, description="Case-insensitive substring of url or title. Omit to list all."),
    ],
    kind: Annotated[
        str | None,
        Field(default=None, description="Exact tag: `platform-api`, `spec`, `rfc` or `issue`."),
    ],
    limit: Limit = _tools.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Which node(s) depend on a piece of external documentation. Use when an SDK
    or spec changes and you need every place the code relies on it. Each hit has
    the `path` / `symbol` it narrows to, if any."""
    return _read("kg_find_by_reference", {"query": query, "kind": kind, "limit": limit, "offset": offset})


@with_config(ConfigDict(extra="forbid"))
class LinkSpec(TypedDict, total=False):
    target: Required[
        Annotated[str, Field(description="`<peer-db>#<node>`, relative to `knowledge/`.")]
    ]
    kind: str


@with_config(ConfigDict(extra="forbid"))
class ReferenceSpec(TypedDict, total=False):
    url: Required[str]
    kind: str
    title: str
    path: Annotated[str, Field(description="Must equal an anchor's path.")]
    symbol: Annotated[str, Field(description="With `path`; must match an anchor.")]


# Every field skips validation and unknown keys pass through: `edits.upsert_node`
# does its own checks and refuses an unknown key with a did-you-mean hint, which
# pydantic's `extra="forbid"` error would replace. The published schema still
# says `additionalProperties: false`.
@with_config(ConfigDict(extra="allow", json_schema_extra={"additionalProperties": False}))
class NodePatch(TypedDict, total=False):
    id: Required[
        Annotated[
            SkipValidation[str],
            Field(description="A new node also needs `kind`."),
        ]
    ]
    kind: SkipValidation[str]
    description: SkipValidation[str]
    section: SkipValidation[str]
    parity: Annotated[
        SkipValidation[str | None],
        Field(description="`matched`, `divergent` or `<codebase>-only`."),
    ]
    counterpart: Annotated[
        SkipValidation[str | None],
        Field(description="`<peer-graph-path>#<peer-node-id>`."),
    ]
    divergence: SkipValidation[str | None]
    anchors: Annotated[
        SkipValidation[list[str]],
        Field(description='`["path/File.kt#Symbol"]`: symbols, never line numbers.'),
    ]
    edges: SkipValidation[list[str]]
    external_links: SkipValidation[list[LinkSpec]]
    references: SkipValidation[list[ReferenceSpec]]
    rebaseline: Annotated[
        SkipValidation[bool],
        Field(
            description=(
                "`true` re-hashes the node's anchored files for every node that anchors "
                "them. Set it only after checking all of those nodes."
            )
        ),
    ]


@mcp.tool(annotations={**_WRITE, "destructiveHint": False, "idempotentHint": True}, output_schema=_OBJECT)
def kg_upsert_node(
    nodes: Annotated[list[NodePatch], Field(description="One object per node.")],
) -> dict[str, Any] | ToolResult:
    """Atomic: if any node is rejected nothing is written (`ok: false`,
    `written: false`). Creates or updates nodes for targeted fixes (bulk work:
    `kg_cli.py export`, edit, `build`). Only supplied keys change; `null` clears parity,
    counterpart and divergence. `anchors`, `edges`, `external_links` and
    `references` replace the whole list, so read the node first to append.
    Returns each row changed, before and after."""
    return _write(_edits.upsert_node, nodes)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_delete_node(
    ids: Annotated[list[str], Field(description="Node ids.")],
    dry_run: Annotated[
        bool, Field(description="True previews and writes nothing; pass false to apply.")
    ] = True,
    cascade_inbound: Annotated[
        bool,
        Field(description="Also remove other nodes' edges into these; otherwise they block the delete."),
    ] = False,
) -> dict[str, Any] | ToolResult:
    """Delete nodes; previews unless `dry_run=false`. A node's anchors, outbound
    edges and external links go with it. Edges from other nodes into it block the
    delete unless `cascade_inbound=true`. Rejected whole if it would break the graph."""
    return _write(_edits.delete_node, ids, dry_run=dry_run, cascade_inbound=cascade_inbound)


@mcp.tool(annotations=_WRITE, output_schema=_OBJECT)
def kg_add_link(
    node_id: Annotated[str, Field(description="Code node id.")],
    target: Annotated[
        str,
        Field(description="`<db-file>#<node-id>`, relative to `knowledge/`, never absolute."),
    ],
    kind: Annotated[
        str,
        Field(description="E.g. `implements`, `presented-by`, `tests`, `documents`. Empty means unspecified."),
    ] = "",
) -> dict[str, Any] | ToolResult:
    """Point a code node at a node in another committed graph in this repo,
    typically a screen in `baselines/baseline.db`. Inverse of `kg_find_by_link`.
    Refused if the peer graph is present and lacks that node."""
    return _write(_edits.add_link, node_id, target, kind)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_remove_link(
    node_id: Annotated[str, Field(description="Code node id.")],
    target: Annotated[
        str, Field(description="As `kg_node` lists it.")
    ],
) -> dict[str, Any] | ToolResult:
    """Remove one cross-graph link from a node; the node, anchors and edges stay.
    Refused when the node has no link to `target`. Not for a documentation
    reference (`kg_remove_reference`) or an edge between code nodes
    (`kg_upsert_node` with the new `edges`). Returns the change, not the node."""
    return _write(_edits.remove_link, node_id, target)


@mcp.tool(annotations=_WRITE, output_schema=_OBJECT)
def kg_add_reference(
    node_id: Annotated[str, Field(description="Code node id.")],
    url: Annotated[str, Field(description="Documentation URL.")],
    kind: Annotated[
        str, Field(default="", description="`platform-api`, `spec`, `rfc` or `issue`.")
    ],
    title: Annotated[str, Field(default="", description="Label.")],
    path: Annotated[
        str | None,
        Field(default=None, description="Must equal the path of one of the node's anchors."),
    ],
    symbol: Annotated[
        str | None,
        Field(default=None, description="With `path`; together they must equal one of the node's anchors."),
    ],
) -> dict[str, Any] | ToolResult:
    """Record where a fact a code node depends on is documented: an API page, spec,
    RFC or issue. Inverse of `kg_find_by_reference`. A `path` / `symbol` that
    matches none of the node's anchors is refused, so read the node first. Not for
    a link to a node in another graph (`kg_add_link`)."""
    return _write(_edits.add_reference, node_id, url, kind, title, path, symbol)


@mcp.tool(annotations=_DESTRUCTIVE, output_schema=_OBJECT)
def kg_remove_reference(
    node_id: Annotated[str, Field(description="Code node id.")],
    url: Annotated[str, Field(description="As `kg_node` lists it.")],
    path: Annotated[
        str | None,
        Field(default=None, description="Removes only the reference with exactly this `path` and `symbol`."),
    ],
    symbol: Annotated[
        str | None,
        Field(default=None, description="See `path`."),
    ],
) -> dict[str, Any] | ToolResult:
    """Remove a node's references to `url`: all of them, or with `path` / `symbol`
    only the one with that exact narrowing. Refused when nothing matches. Not for
    a link to a node in another graph (`kg_remove_link`). Returns the change, not
    the node."""
    return _write(_edits.remove_reference, node_id, url, path, symbol)

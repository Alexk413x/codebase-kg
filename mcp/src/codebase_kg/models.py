"""Graph data model — plain dataclasses, stdlib only.

A KG file parses into one `Graph`: a `Header` (the config block) plus a list of
`Node`s. See ../../SCHEMA.md for the on-disk shape these mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    """One component of the codebase. Mirrors a node key/value table (SCHEMA.md §4)."""

    id: str
    kind: str = ""
    anchors: list[str] = field(default_factory=list)  # "path#Symbol" pointers
    summary: str = ""
    edges: list[str] = field(default_factory=list)  # ids of related nodes (intra-KG)
    parity: str | None = None  # matched | divergent | <codebase>-only
    counterpart: str | None = None  # "<peer-kg-path>#<node-id>"
    divergence: str | None = None  # one line; only when parity == divergent
    updated: str | None = None  # YYYY-MM-DD this node was last added/verified vs source
    section: str = ""  # the "### SECTION" the node was grouped under (display only)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "anchors": self.anchors,
            "summary": self.summary,
            "edges": self.edges,
            "section": self.section,
        }
        if self.updated is not None:
            d["updated"] = self.updated
        if self.parity is not None:
            d["parity"] = self.parity
        if self.counterpart is not None:
            d["counterpart"] = self.counterpart
        if self.divergence is not None:
            d["divergence"] = self.divergence
        return d


@dataclass
class Header:
    """The KG config/provenance block (SCHEMA.md §3)."""

    codebase: str = ""
    root: str = ""
    counterpart: str | None = None  # peer KG file path, for parity
    language: str | None = None
    refreshed: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "codebase": self.codebase,
            "root": self.root,
            "refreshed": self.refreshed,
        }
        if self.counterpart is not None:
            d["counterpart"] = self.counterpart
        if self.language is not None:
            d["language"] = self.language
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class Graph:
    """A parsed KG: header + nodes, with an id index and an inbound-edge index."""

    header: Header
    nodes: list[Node]
    path: str = ""  # the source KG file path (absolute, when known)

    def __post_init__(self) -> None:
        self._by_id: dict[str, Node] = {}
        self._inbound: dict[str, list[str]] = {}
        # SCHEMA.md §4: ids must be unique. Later nodes win the index, but the
        # collisions are recorded so kg_validate can report them.
        self.duplicate_ids: list[str] = []
        for n in self.nodes:
            if n.id in self._by_id and n.id not in self.duplicate_ids:
                self.duplicate_ids.append(n.id)
            self._by_id[n.id] = n
        for n in self.nodes:
            for e in n.edges:
                self._inbound.setdefault(e, []).append(n.id)

    def by_id(self, node_id: str) -> Node | None:
        return self._by_id.get(node_id)

    def inbound(self, node_id: str) -> list[str]:
        """ids of nodes that list `node_id` in their edges."""
        return list(self._inbound.get(node_id, []))

    @property
    def ids(self) -> set[str]:
        return set(self._by_id)

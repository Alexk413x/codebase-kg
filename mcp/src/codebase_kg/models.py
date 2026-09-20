"""Graph data model — plain dataclasses, stdlib only.

These mirror the tables in `schema.py`. They are the *write* shape: `writer.py`
takes `Node`s and `Meta`, and the read path materializes them back out of SQL
for the handful of tools that want a whole node.

Note the field is `description`, not `summary`. It is one short line saying what
the component is and does — capped at `MAX_DESCRIPTION` and scrubbed of ticket
refs, dates and changelog narrative (see `clean.py`), because that content
duplicated git and was the entire source of the old maintenance burden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .links import ExternalLink


@dataclass(frozen=True)
class Anchor:
    """A symbol pointer into source: `path#Symbol` (SCHEMA.md §4.1).

    `symbol` is None for a whole-file anchor (a manifest, a config file).

    `path` is always posix-separated. Normalizing here — at the single point
    where an anchor is constructed from text — is what lets every reader compare
    `anchor.path` directly. The alternative is each of the nine read sites
    re-normalizing, two of which are vendored files that cannot share a helper.
    A CHECK constraint backs this up so a hand-written row cannot break it.

    The digest of the anchored file lives in the `source` table, keyed by path —
    it is a fact about the file rather than about any one anchor into it.
    """

    path: str
    symbol: str | None = None

    def __post_init__(self) -> None:
        # In __post_init__ rather than in parse(), so direct construction
        # normalizes too — otherwise the invariant would hold only for anchors
        # that happened to come from text.
        if "\\" in self.path:
            object.__setattr__(self, "path", self.path.replace("\\", "/"))

    @classmethod
    def parse(cls, text: str) -> Anchor:
        path, sep, symbol = text.strip().strip("`").strip().partition("#")
        return cls(path=path.strip(), symbol=(symbol.strip() or None) if sep else None)

    @property
    def base(self) -> str:
        """The filename alone — indexed, so a bare-name lookup is an equality probe."""
        return self.path.rsplit("/", 1)[-1]

    def __str__(self) -> str:
        return f"{self.path}#{self.symbol}" if self.symbol else self.path


class ReferenceFormatError(ValueError):
    """A reference that is not `{"url": ...}` plus optional narrowing."""


@dataclass(frozen=True)
class Reference:
    """Where a fact this node depends on is documented: one row of `reference`.

    `path` and `symbol` narrow the reference to one file or one function, and
    they are the anchor table's own vocabulary on purpose: a narrowing must name
    one of the node's anchors exactly (`problem`), and anchors are what
    `kg_validate` already resolves against source. A narrowing that named
    anything else would be free text nobody checks.
    """

    url: str
    kind: str = ""  # platform-api | spec | rfc | issue — a short tag, free text
    title: str = ""
    path: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        for name in ("url", "kind", "title"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        path = (self.path or "").strip().replace("\\", "/") or None
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "symbol", (self.symbol or "").strip() or None)

    @classmethod
    def parse(cls, raw: object) -> Reference:
        """Accept a bare URL or `{"url", "kind", "title", "path", "symbol"}`."""
        if isinstance(raw, str):
            ref = cls(url=raw)
        elif isinstance(raw, dict):
            for key in ("url", "kind", "title", "path", "symbol"):
                if raw.get(key) is not None and not isinstance(raw[key], str):
                    raise ReferenceFormatError(f"reference {raw!r}: `{key}` must be a string")
            ref = cls(
                url=raw.get("url") or "",
                kind=raw.get("kind") or "",
                title=raw.get("title") or "",
                path=raw.get("path"),
                symbol=raw.get("symbol"),
            )
        else:
            raise ReferenceFormatError(f"reference {raw!r} must be a URL string or an object")
        if not ref.url:
            raise ReferenceFormatError(f"reference {raw!r} has no `url`")
        return ref

    @property
    def narrowing(self) -> str:
        """`path#symbol`, `path`, or '' — the same spelling an anchor uses."""
        if self.path is None:
            return f"#{self.symbol}" if self.symbol else ""
        return f"{self.path}#{self.symbol}" if self.symbol else self.path

    def problem(self, anchors: list[Anchor]) -> str | None:
        """Why this reference cannot sit on a node with `anchors`, or None.

        The one expression of the narrowing rule; the writer, the edit tools and
        `kg_validate` all ask here so they cannot disagree.
        """
        if not self.url:
            return "a reference has no url"
        if self.path is None and self.symbol is None:
            return None
        if self.path is None:
            return f"reference {self.url} narrows to symbol '{self.symbol}' without a path"
        if self.symbol is None:
            if any(a.path == self.path for a in anchors):
                return None
        elif any(a.path == self.path and a.symbol == self.symbol for a in anchors):
            return None
        return (
            f"reference {self.url} narrows to '{self.narrowing}', "
            "which is not one of this node's anchors"
        )

    def as_dict(self) -> dict[str, str]:
        d = {"url": self.url}
        for name in ("kind", "title", "path", "symbol"):
            value = getattr(self, name)
            if value:
                d[name] = value
        return d


@dataclass
class Node:
    """One component of the codebase."""

    id: str
    kind: str = ""
    description: str = ""
    anchors: list[Anchor] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)  # ids of related nodes (intra-graph)
    section: str = ""  # free-text grouping, display only
    parity: str | None = None  # matched | divergent | <codebase>-only
    counterpart: str | None = None  # "<peer-graph-path>#<node-id>"
    divergence: str | None = None  # one line; only when parity == divergent
    # Pointers into a *different kind* of graph — a screen graph, a docs graph.
    # Distinct from `counterpart`, which is parity between two codebases and
    # cannot express this without giving up the invariant it enforces. See
    # cartographer's docs/GRAPH-LINKS.md; the mechanism is shared by copy.
    links: list[ExternalLink] = field(default_factory=list)
    # Where the platform facts this node depends on are documented. In author
    # order, like anchors.
    references: list[Reference] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "anchors": [str(a) for a in self.anchors],
            "edges": self.edges,
            "section": self.section,
        }
        if self.parity is not None:
            d["parity"] = self.parity
        if self.counterpart is not None:
            d["counterpart"] = self.counterpart
        if self.divergence is not None:
            d["divergence"] = self.divergence
        # Omitted when empty, like the parity fields: almost no node has one,
        # and an `"external_links": []` on every node in a large graph is pure
        # noise in a document a human reviews as a diff.
        if self.links:
            d["external_links"] = [link.as_dict() for link in self.links]
        if self.references:
            d["references"] = [ref.as_dict() for ref in self.references]
        return d


@dataclass
class Meta:
    """Provenance + the config the tools read (SCHEMA.md §3).

    `generated` is when the artifact was last built. It is provenance, not a
    freshness contract — nothing gates on it. Staleness is answered by checking
    anchors against source, which is a fact about the code rather than a date
    somebody remembered to bump.
    """

    codebase: str = ""
    root: str = ""
    counterpart: str | None = None  # peer code_graph.db path, for parity
    language: str | None = None
    generated: str = ""  # YYYY-MM-DD
    # Which files the graph is expected to cover, and which are excused. Both
    # are glob patterns relative to `root` (see coverage.py). Declaring them is
    # what makes a coverage gap reportable instead of invisible.
    covers: list[str] = field(default_factory=list)
    exempt: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # `.` and `` both mean the whole repo and must reach the readers as the
        # same value. A literal `.` is truthy but prefixes no repo-relative path,
        # so every `rel.startswith(root + "/")` fails and the graph matches
        # nothing. It hid because the MCP tools JOIN (`repo / "."` is `repo`)
        # while the hooks COMPARE: a real graph validated clean with both of its
        # hooks mute. Normalized here so no graph can be built carrying it.
        self.root = "" if self.root.strip() == "." else self.root

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "codebase": self.codebase,
            "root": self.root,
            "generated": self.generated,
        }
        if self.counterpart is not None:
            d["counterpart"] = self.counterpart
        if self.language is not None:
            d["language"] = self.language
        if self.covers:
            d["covers"] = list(self.covers)
        if self.exempt:
            d["exempt"] = list(self.exempt)
        return d

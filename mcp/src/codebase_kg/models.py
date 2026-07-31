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

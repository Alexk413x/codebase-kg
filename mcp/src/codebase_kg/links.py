"""Cross-graph links -- one table and a handful of functions, copied not imported.

**This file is a copy of the mechanism, not of the text.** `cartographer` and
`android-driver` keep their own copies, which differ. The shared contract is
`EXTERNAL_LINK_DDL` and the `<db-file>#<node-id>` target format. Any future
graph adopts the mechanism the same way: copy
this module, splice `EXTERNAL_LINK_DDL` into that graph's DDL with the foreign
key pointed at that graph's own entity table, done. The specification is
`docs/GRAPH-LINKS.md` in the cartographer repo.

Copied rather than packaged, and this paragraph exists so the decision is not
quietly reversed by someone tidying up. It was measured before it was made: the
two graph stores share five function names out of twenty-eight and twenty-nine,
and three of those are `__init__`, `_hydrate` and `meta`. Everything substantive
is domain-specific -- cartographer routes between screens with Dijkstra and
resolves per-platform bindings, codebase-kg tracks parity between codebases. A
shared library would extract this one file and nothing else, and would buy a
third repository that both plugins must version against. That is the failure it
prevents: a one-line fix here becoming a release, a version bump and two
lockfile updates before anybody can use it.

## The mechanism

`external_link` holds pointers from a node in THIS graph to a node in ANOTHER
graph. It exists as its own table because the natural field cannot carry the
link: codebase-kg's `counterpart` is CHECK-constrained to parity between two
*codebases* and physically cannot hold a screen id. Relaxing that constraint to
fit a second use case would destroy the invariant it enforces, so this sits
beside it instead.

A target is `<db-file>#<node-id>` where `<db-file>` is relative to the project's
`knowledge/` directory -- never absolute. An absolute path is a fact about one
machine; committed into a shared artifact it breaks on the next clone and reads
as a dangling link to everyone except its author.

`kind` names what the link means from the source's side -- `implements`,
`tests`, `documents`. Empty is allowed and means unspecified; it is not a
missing value to be filled in later, it is the honest state of a link nobody has
characterised.

## Resolution, and why the two failures are graded differently

* The peer database is **absent** -> warning. Either graph must be fully usable
  alone, which is what makes each plugin independently installable. A repo with
  no code graph is a supported configuration, not a broken one.
* The peer exists but the node id is **missing** -> error. That is a genuine
  dangling pointer, and it fails silently at exactly the moment it is needed:
  when something is already broken and someone is following the link to find out
  why.

The peer's entity table is **looked up, not assumed**. This module used to end
`resolve` with `SELECT 1 FROM node WHERE id = ?`, which is this graph's table
name and nobody else's. Measured 2026-08-10 against real artifacts: v3
`cartographer_graph.db` has no `node` table -- its entity is `screen` -- and
`driver_graph.db` calls its entity `action`, so following a link into either
raised, was caught, and reported `PEER_UNREADABLE`. Four of six resolutions
across the three graphs were wrong, and every one of them failed *soft*: a
genuinely dangling link out of this graph into one of those could never be an
error, only a permanent shrug. `meta.node_table` names the entity, with a probe
for the artifacts committed before that key existed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Bumped whenever the table, the URI convention or the resolution rules change.
#: Every copy carries it, so "are these two graphs speaking the same mechanism?"
#: is one comparison rather than a diff of two files in two repositories.
LINKS_SPEC_VERSION = 1

#: What `<db-file>` is resolved against. The knowledge directory, not the repo
#: root: every graph in a project lives there together, so a peer is named by
#: its filename alone and nothing has to know how deep the directory sits or
#: what the repo above it is called.
KNOWLEDGE_DIR = "knowledge"

SEPARATOR = "#"

#: The `meta` key naming a graph's own entity table, so a peer can resolve into
#: it without knowing what it is. Written by every graph that carries this module.
NODE_TABLE_KEY = "node_table"

#: Entity tables of the graphs that predate `NODE_TABLE_KEY`, tried in order when
#: a peer does not declare one. Dropping this list would make every artifact
#: committed before today unresolvable, and a committed artifact that stops
#: opening is the failure this whole design is arranged to avoid.
_LEGACY_NODE_TABLES = ("node", "screen", "action")

# The index is not optional, and it is the reason this is a table rather than a
# column. The reverse question -- a file changed, which screens does that
# affect? -- is the one that gets asked in practice, and it reads `target`,
# which is not the leading column of the primary key. Without the index every
# such lookup is a scan over every link in the graph.
EXTERNAL_LINK_DDL = """\
CREATE TABLE external_link (
    node_id TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    target  TEXT NOT NULL,   -- "<db-file>#<node-id>"
    kind    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, target),
    CHECK (target <> ''),
    CHECK (instr(target, '#') > 1)
) WITHOUT ROWID;

CREATE INDEX external_link_target ON external_link(target);
"""

TABLE = "external_link"


class LinkError(ValueError):
    """A target that is not `<db-file>#<node-id>`."""


def normalize_target(target: str) -> str:
    """Canonical form: posix separators, relative to `knowledge/`.

    Two rewrites, each closing a way the same link ends up stored as two
    different strings:

    * Backslashes become forward slashes. A link written on Windows and read on
      a Mac has to be the same bytes, or the reverse lookup -- an indexed
      equality probe -- silently returns nothing.
    * A leading `knowledge/` is stripped. Links predating this convention were
      repo-root relative (`knowledge/code_graph.db#x`); left alone such a target
      resolves to `knowledge/knowledge/code_graph.db`, which never exists, and
      the check quietly downgrades from "dangling link" (an error) to "peer
      absent" (a warning). A check that softens itself is worse than one that is
      loudly wrong.
    """
    out = str(target).replace("\\", "/").strip()
    prefix = KNOWLEDGE_DIR + "/"
    out = out.removeprefix(prefix)
    return out


def split_target(target: str) -> tuple[str, str]:
    """`"code_graph.db#rpn_screen"` -> `("code_graph.db", "rpn_screen")`.

    Raises `LinkError` when either half is empty. The table's CHECK rejects a
    missing or leading `#`, but SQLite's `instr` cannot also express "and
    something follows it" -- so `code_graph.db#` is writable, and it resolves to
    a lookup for the empty id: a link that can never match anything and never
    says why.
    """
    db_file, sep, node_id = normalize_target(target).partition(SEPARATOR)
    if not sep or not db_file or not node_id:
        raise LinkError(f"{target!r} is not '<db-file>#<node-id>'")
    return db_file, node_id


def format_target(db_file: str, node_id: str) -> str:
    """Build a canonical target from its two halves."""
    return normalize_target(f"{db_file}{SEPARATOR}{node_id}")


@dataclass(frozen=True, order=True)
class ExternalLink:
    """One row of `external_link`, without its owning node."""

    target: str
    kind: str = ""

    def __post_init__(self) -> None:
        # Normalized at the single point where a link is constructed, so every
        # reader may compare `target` directly. The alternative is each read
        # site re-normalizing, and the first one that forgets makes the reverse
        # lookup miss for reasons nobody can see in the data.
        canonical = normalize_target(self.target)
        if canonical != self.target:
            object.__setattr__(self, "target", canonical)

    @property
    def db_file(self) -> str:
        return split_target(self.target)[0]

    @property
    def node_id(self) -> str:
        return split_target(self.target)[1]

    def as_dict(self) -> dict[str, str]:
        return {"target": self.target, "kind": self.kind}

    @classmethod
    def parse(cls, raw: object) -> ExternalLink:
        """Accept either `"db#id"` or `{"target": ..., "kind": ...}`.

        A bare string is the shape hand-authored JSON reaches for, and rejecting
        it would fail a whole build over punctuation. It means kind-unspecified,
        which is exactly what a link written without a kind is.
        """
        if isinstance(raw, str):
            return cls(target=raw)
        if isinstance(raw, dict):
            target = raw.get("target")
            if not isinstance(target, str) or not target.strip():
                raise LinkError(f"external link {raw!r} has no `target`")
            kind = raw.get("kind") or ""
            if not isinstance(kind, str):
                raise LinkError(f"external link {target!r}: `kind` must be a string")
            return cls(target=target.strip(), kind=kind.strip())
        raise LinkError(f"external link {raw!r} must be a string or an object")


def dedupe(links: Iterable[ExternalLink]) -> list[ExternalLink]:
    """Sorted and unique -- and a named error when one target carries two kinds.

    `(node_id, target)` is the primary key, so a node may call a target
    `implements` or `tests` but not both. Caught here rather than as a bare
    IntegrityError from the insert, which names neither the target nor the two
    kinds that collided.
    """
    by_target: dict[str, ExternalLink] = {}
    for link in sorted(set(links)):
        seen = by_target.get(link.target)
        if seen is not None and seen.kind != link.kind:
            raise LinkError(
                f"{link.target!r} is linked twice with different kinds: "
                f"{seen.kind!r} and {link.kind!r}"
            )
        by_target.setdefault(link.target, link)
    return list(by_target.values())


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def has_link_table(con: sqlite3.Connection) -> bool:
    """Does this graph carry `external_link` at all?

    Probed rather than assumed, so a graph written before the table existed
    still reads. A reader that required the table would refuse every artifact
    committed before the plugin was updated, and a committed artifact that stops
    opening is the one failure this whole design is arranged to avoid.
    """
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def links_for(con: sqlite3.Connection, node_id: str) -> list[ExternalLink]:
    """Every outbound link on one node, in a stable order.

    Ordered so a rebuild of unchanged input produces an identical file -- a
    no-op refresh has to leave an empty git diff or every rebuild looks like a
    change and review stops meaning anything.
    """
    return [
        ExternalLink(target=r[0], kind=r[1])
        for r in con.execute(
            f"SELECT target, kind FROM {TABLE} WHERE node_id = ? ORDER BY target, kind",
            (node_id,),
        )
    ]


def all_links(con: sqlite3.Connection) -> list[tuple[str, ExternalLink]]:
    """`(node_id, link)` for the whole graph, for validation and export."""
    return [
        (r[0], ExternalLink(target=r[1], kind=r[2]))
        for r in con.execute(
            f"SELECT node_id, target, kind FROM {TABLE} ORDER BY node_id, target, kind"
        )
    ]


def nodes_linking_to(
    con: sqlite3.Connection, target: str, *, kind: str | None = None
) -> list[str]:
    """The reverse question: which nodes here point at that node over there?

    This is the lookup `external_link_target` exists for. It is also the half
    that makes two graphs mutually useful rather than merely adjacent: the link
    is stored once, on one side, and the other direction is a query.

    Accepts a full `<db-file>#<node-id>` or a bare node id, because the tools
    that hand you one -- `kg_find_by_path`, `map_find_by_code` -- return the
    latter. A bare id is matched against the fragment with `substr`, not with
    `LIKE '%#id'`: an id containing `%` or `_` would silently match the wrong
    rows under LIKE, and neither form can use the index anyway.
    """
    args: list[object] = []
    if SEPARATOR in target:
        where = "target = ?"
        args.append(normalize_target(target))
    else:
        where = f"substr(target, instr(target, '{SEPARATOR}') + 1) = ?"
        args.append(target.strip())
    if kind is not None:
        where += " AND kind = ?"
        args.append(kind)
    return [
        r[0]
        for r in con.execute(
            f"SELECT node_id FROM {TABLE} WHERE {where} ORDER BY node_id", tuple(args)
        )
    ]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def insert(con: sqlite3.Connection, node_id: str, link: ExternalLink) -> None:
    """Write one link. Plain INSERT: a duplicate is an authoring bug, not noise."""
    con.execute(
        f"INSERT INTO {TABLE} (node_id, target, kind) VALUES (?,?,?)",
        (node_id, link.target, link.kind),
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


class Resolution(str, Enum):
    """What happened when a link was followed."""

    OK = "ok"
    #: Not `<db-file>#<node-id>`. Nothing can be done with it.
    MALFORMED = "malformed"
    #: The peer graph is not in this repo. Supported: each graph stands alone.
    PEER_ABSENT = "peer-absent"
    #: The file is there but is not a graph we can read. Unknown, not broken.
    PEER_UNREADABLE = "peer-unreadable"
    #: The peer graph is readable and does not contain that node.
    DANGLING = "dangling"

    @property
    def is_error(self) -> bool:
        """Only a link that is *provably* broken fails a validation run.

        The two soft outcomes are soft for the same reason: we could not check.
        Failing on them would mean a repo without the peer plugin installed can
        never pass validation, which would make an optional dependency mandatory
        by the back door.
        """
        return self in (Resolution.MALFORMED, Resolution.DANGLING)


def peer_node_table(con: sqlite3.Connection) -> str | None:
    """Name the entity table of an already-open peer graph, or None.

    Reads `meta.node_table` first, because a graph naming its own entity is the
    only answer that stays correct when a fourth graph arrives. The fallback
    probe exists for the artifacts committed before this key did.
    """
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key = ?", (NODE_TABLE_KEY,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        return str(row[0])
    for name in _LEGACY_NODE_TABLES:
        probe = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if probe is not None:
            return name
    return None


def resolve(knowledge_dir: str | Path, target: str) -> Resolution:
    """Follow a link into its peer graph and report what was found there.

    Reading the peer is a file read in the same repo, not a call into another
    plugin -- which is the entire point of committing the graph as an artifact.
    """
    try:
        db_file, node_id = split_target(target)
    except LinkError:
        return Resolution.MALFORMED

    peer = Path(knowledge_dir) / db_file
    if not peer.is_file():
        return Resolution.PEER_ABSENT
    try:
        con = sqlite3.connect(f"file:{peer.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return Resolution.PEER_UNREADABLE
    try:
        table = peer_node_table(con)
        if table is None:
            return Resolution.PEER_UNREADABLE
        row = con.execute(
            f"SELECT 1 FROM {table} WHERE id = ?",  # name from the peer's own schema
            (node_id,),
        ).fetchone()
    except sqlite3.Error:
        # A file we cannot read as a graph is unknown, not broken. Calling it a
        # dangling link would send someone hunting a bug that is not there.
        return Resolution.PEER_UNREADABLE
    finally:
        con.close()
    return Resolution.OK if row is not None else Resolution.DANGLING

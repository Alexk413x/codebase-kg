"""Targeted writes to a committed `code_graph.db`. Stdlib only.

`export -> edit the JSON -> build` stays. It is the right shape for bulk work -- a
parity sweep over every node, a restructuring that touches half the edges,
anything where reviewing the diff before building it is the point. What it is
not is a reasonable way to fix one node's description: regenerating the whole
artifact to change one string costs a full read/write cycle and leaves a scratch
JSON file in the repo root that somebody has to remember to delete.

This module is the other half. Three properties make a write safe enough to
point at a committed artifact:

**It lands whole or not at all.** Every operation runs against a private copy of
the file, inside one SQLite transaction, and the copy is moved over the original
only once everything has passed. A CHECK violated by the third of five nodes
leaves the committed graph *untouched* rather than rolled back -- nothing ever
opened it for writing. This is the mechanism `writer.build` already uses, and
reusing it keeps `CodeGraph` strictly read-only: the artifact is opened `mode=ro`
by every reader in the package, and a bug in an edit still cannot mutate it.

**It is validated, not merely constrained.** The CHECKs reject a malformed row.
They cannot see that deleting a node stranded the counterpart the peer graph
links back to, because that fact lives in another file. So `kg_validate` -- the
same function the tool calls, not a reimplementation -- runs against the copy and
the write is refused if it introduced a finding the graph did not already have.
Not "if the graph is clean": a real graph usually carries findings, and demanding
zero would make writes impossible on exactly the graphs that need editing.

**It says what it did.** The round trip gave review for free -- you read the JSON
diff before building it. A tool that mutates silently takes that away, so every
operation returns the rows it touched, field by field, before and after.

Nothing here holds the graph open across a call. Windows refuses to replace a
file anyone has open, which is why the server does not cache a handle; a write
that kept one would make the next `/codebase-kg:refresh` fail.
"""

from .edit_core import Change, EditError, apply
from .edit_delete import delete_node
from .edit_links import add_link, add_reference, remove_link, remove_reference
from .edit_nodes import upsert_node
from .store import StoreError

__all__ = [
    "Change",
    "EditError",
    "StoreError",
    "add_link",
    "add_reference",
    "apply",
    "delete_node",
    "remove_link",
    "remove_reference",
    "upsert_node",
]

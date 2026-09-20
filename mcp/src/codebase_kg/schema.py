"""The on-disk schema for a committed `code_graph.db`. Stdlib only.

One SQLite file per repo, committed. The DDL below is the *whole* contract —
`writer.py` creates it, `store.py` reads it, and nothing else may assume shape.

The constraints are load-bearing, not decoration. They move an entire class of
defect from "`kg_validate` reports it afterwards" to "the write fails":

- `edge.dst` is a foreign key → **a dangling edge cannot be written.**
- `anchor.node_id` is a foreign key → an orphan anchor cannot be written.
- `node.id` is the primary key → duplicate ids cannot be written.
- The `parity`/`counterpart`/`divergence` CHECKs encode SCHEMA.md §8 in the
  file itself → an inconsistent parity triple cannot be written.
- `length(description) <= 240` → the field cannot grow back into a summary.

Everything a writer must satisfy is therefore enforced by the store, so a
regeneration either lands whole or rolls back — there is no half-written graph.
"""

from __future__ import annotations

import re
import sqlite3

from .links import EXTERNAL_LINK_DDL

# Bumped whenever the DDL below changes in a way a reader must know about.
#
# `external_link` did NOT bump it, and that is the point of being additive: a v3
# graph simply has no such table, the reader probes rather than assumes, and an
# older server reading a newer graph ignores the links — which degrades to "no
# cross-graph links", the same state as a graph that has none. Bumping would
# have made every existing committed graph refuse to open in exchange for
# nothing.
#
# `reference` DID bump it, 3 -> 4, and the difference is what an older server
# does to the rows. It cannot see them, so its next export -> build writes a
# graph without them and reports success. The stamp makes that server refuse
# the file and name the fix instead. `MIN_READABLE_VERSION` is what keeps the
# bump additive in the other direction: a v3 graph is a v4 graph with no
# `reference` table, the reader probes for it, and every committed v3 artifact
# still opens.
SCHEMA_VERSION = 4
MIN_READABLE_VERSION = 3

REFERENCE_TABLE = "reference"

# The table a peer graph must query to check that one of our ids exists. Written
# into `meta` under `links.NODE_TABLE_KEY` because a peer holds no copy of this
# schema: before it was declared, every graph guessed `node`, which is right here
# and wrong in cartographer (`screen`) and android-driver (`action`).
NODE_TABLE = "node"

# 'CKG1' as a big-endian int32, stamped into the SQLite header so `file(1)` and
# our own tooling can identify the artifact without opening it.
APPLICATION_ID = 0x434B4731

# Hard cap on a node description. The point of the cap is that it makes the old
# failure mode impossible: at 240 chars there is no room for a changelog.
# Interpolated into the DDL below so the constant and the CHECK cannot drift.
MAX_DESCRIPTION = 240

# CamelCase / PascalCase boundary: `QuartzUploadWorker`, `HTTPSConnection`.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")


def split_identifier(token: str) -> list[str]:
    """An identifier plus its component words: `QuartzUploadWorker` → the whole
    token, then `Quartz`, `Upload`, `Worker`.

    This lives beside the `node_fts` definition because it is half of that
    table's contract. The FTS tokenizer breaks on punctuation but not on case,
    so `QuartzUploadWorker` indexes as one opaque token and a search for
    "quartz upload" would miss it entirely. The writer emits these split forms
    into the index and the reader expands the query the same way — **both sides
    must use this one function**, because if they ever disagree search silently
    returns fewer hits rather than failing.
    """
    if not token:
        return []
    out = [token]
    for part in _NON_WORD.split(token):
        if not part:
            continue
        pieces = _CAMEL.split(part)
        if len(pieces) > 1:
            out.extend(pieces)
    return out

# Fixed so a rebuild of identical input yields an identical file (see writer.py).
PAGE_SIZE = 4096

# Where a platform fact a node depends on is documented. Not `external_link`:
# that targets a node in another graph and is resolved by opening that graph; a
# URL has no node to resolve to.
#
# Keyed `(node_id, ord)` like `anchor`. One node may cite one page for two
# different symbols, so `url` cannot be part of a unique key, and `path` and
# `symbol` are nullable, which a WITHOUT ROWID primary key does not allow.
#
# No index on `url`: the reverse question is asked by substring ("everything
# under developer.android.com/reference/android/view"), which an index cannot
# serve, over a table far smaller than `node`.
#
# `path` and `symbol` carry the same CHECKs as `anchor`, because a narrowing
# must equal one of the node's anchors. That equality is not a foreign key:
# anchors are replaced wholesale on edit and a NULL symbol never matches under
# FK rules, so `clean.node_problems` refuses the write and `kg_validate`
# reports whatever reaches the file another way.
#
# Separate from `DDL` so `edits.py` can add it to a v3 graph in place.
REFERENCE_DDL = """\
CREATE TABLE reference (
    node_id TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    kind    TEXT NOT NULL DEFAULT '',
    title   TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL,
    path    TEXT,
    symbol  TEXT,
    PRIMARY KEY (node_id, ord),
    CHECK (url <> ''),
    CHECK (path IS NULL OR (path <> '' AND instr(path, char(92)) = 0)),
    CHECK (symbol IS NULL OR (symbol <> '' AND symbol NOT GLOB '[0-9]*')),
    CHECK (symbol IS NULL OR path IS NOT NULL)
) WITHOUT ROWID;
"""


def has_reference_table(conn: sqlite3.Connection) -> bool:
    """Probed, not assumed: a v3 graph has no such table and still opens."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (REFERENCE_TABLE,)
    ).fetchone()
    return row is not None


DDL = f"""
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE node (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    section     TEXT NOT NULL DEFAULT '',
    parity      TEXT,
    counterpart TEXT,
    divergence  TEXT,

    CHECK (id <> ''),
    CHECK (kind <> ''),
    CHECK (length(description) <= {MAX_DESCRIPTION}),
    -- SCHEMA.md §8: the only three parity shapes.
    CHECK (parity IS NULL
           OR parity IN ('matched', 'divergent')
           OR parity LIKE '%-only'),
    -- A counterpart link is meaningless without a parity flag...
    CHECK (counterpart IS NULL OR parity IS NOT NULL),
    -- ...divergent must say who and how...
    CHECK (parity <> 'divergent'
           OR (counterpart IS NOT NULL AND divergence IS NOT NULL)),
    -- ...matched must say who...
    CHECK (parity <> 'matched' OR counterpart IS NOT NULL),
    -- ...and <codebase>-only means there is no peer to point at.
    CHECK (parity NOT LIKE '%-only' OR counterpart IS NULL),
    -- `divergence` is the headline for a divergence; nothing else may carry it.
    CHECK (divergence IS NULL OR parity = 'divergent')
) WITHOUT ROWID;

-- Serves the GROUP BY in kg_stats. There is deliberately no index on `parity`
-- or `symbol`: nothing queries them in a way an index could serve, and an index
-- nobody uses is pure weight in a file that is committed on every refresh.
CREATE INDEX node_kind ON node(kind);

-- Anchors are normalized rather than a comma-joined string, so `path` gets its
-- own index — that is what makes "which node owns this file?" a lookup instead
-- of a scan over every node (kg_find_by_path).
--
-- `base` is the filename alone, denormalized so the common "I only know the
-- filename" lookup is an equality probe too. Matching a bare name against
-- `path` would need `LIKE '%/name'`, and a leading wildcard cannot use an index.
CREATE TABLE anchor (
    node_id TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    path    TEXT NOT NULL,
    base    TEXT NOT NULL,
    symbol  TEXT,
    PRIMARY KEY (node_id, ord),
    CHECK (path <> ''),
    -- Paths are stored posix-normalized (see models.Anchor.parse), so readers
    -- can compare them directly instead of each re-normalizing on the way out.
    -- char(92) is a backslash — spelled this way to avoid escaping ambiguity in
    -- both the Python source and the SQL string literal.
    CHECK (instr(path, char(92)) = 0),
    -- SCHEMA.md §4.1: symbol anchors, never line numbers.
    CHECK (symbol IS NULL OR symbol NOT GLOB '[0-9]*')
) WITHOUT ROWID;

-- What the anchored source looked like when the graph was built.
--
-- This is what turns "the symbol still exists" into "the code behind this
-- description has not changed": a rename-preserving refactor that guts a class
-- passes the symbol check cleanly but changes this digest. `kg_validate`
-- reports a mismatch as `changed_since_built` — a prompt to re-read, not a
-- claim that anything is broken.
--
-- Keyed by path rather than carried on `anchor`, because the digest is a fact
-- about the *file*: on the RPN calculator 142 anchors span 91 files, so storing
-- it per anchor would repeat 64 bytes 51 times for nothing. Not authored by
-- hand either — `build.py` computes it from source, since a typed hash would be
-- worse than no hash at all.
CREATE TABLE source (
    path TEXT PRIMARY KEY,
    sha  TEXT NOT NULL,
    CHECK (path <> ''),
    CHECK (instr(path, char(92)) = 0),
    -- A full lowercase SHA-256; a truncated or upper-case digest would compare
    -- unequal forever and read as permanent staleness.
    CHECK (length(sha) = 64 AND sha NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;

CREATE INDEX anchor_path ON anchor(path);
CREATE INDEX anchor_base ON anchor(base);

CREATE TABLE edge (
    src TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    dst TEXT NOT NULL REFERENCES node(id) ON DELETE RESTRICT,
    PRIMARY KEY (src, dst),
    CHECK (src <> dst)
) WITHOUT ROWID;

CREATE INDEX edge_dst ON edge(dst);

-- Pointers OUT of this graph, into another committed graph in the same
-- `knowledge/` directory — a screen graph, a docs graph, whatever comes next.
-- One portable table, identical here and in cartographer, specified in that
-- repo's `docs/GRAPH-LINKS.md` and **copied rather than imported** (links.py
-- says why a shared package would be the wrong trade).
--
-- Additive, and deliberately NOT a change to `counterpart`. That column is
-- CHECK-constrained to a valid parity triple between two *codebases*, and those
-- CHECKs are the reason a half-filled triple is a failed write rather than a
-- validation finding. A link to a screen node has no parity status — "matched"
-- between a Composable and a screen is not a claim anyone can evaluate — and
-- relaxing the constraint so a second, unrelated use case fits would trade a
-- guarantee for a convenience. So this sits beside it. `counterpart` is also
-- one column, and a node routinely links out to several places.
{EXTERNAL_LINK_DDL}

{REFERENCE_DDL}
-- Full-text search, persisted in the file. This is the reason the index is
-- affordable now: it is built once at write time and costs nothing on open,
-- unlike an in-memory index that would have to be rebuilt on every load.
-- `text` carries id + kind + description + anchor paths/symbols, with
-- CamelCase identifiers additionally emitted in split form (see writer.py) so
-- "quartz upload" finds `QuartzUploadWorker`.
CREATE VIRTUAL TABLE node_fts USING fts5(
    node_id UNINDEXED,
    text,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3'
);
"""

# Read-side pragmas. `query_only` is belt-and-braces on top of opening the file
# with `mode=ro`: the MCP server is a reader and must never mutate the artifact.
READ_PRAGMAS = (
    "PRAGMA query_only = ON",
    "PRAGMA foreign_keys = ON",
)

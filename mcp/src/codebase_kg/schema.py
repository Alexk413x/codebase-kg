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

# Bumped whenever the DDL below changes in a way a reader must know about.
SCHEMA_VERSION = 2

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

CREATE INDEX anchor_path ON anchor(path);
CREATE INDEX anchor_base ON anchor(base);

CREATE TABLE edge (
    src TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    dst TEXT NOT NULL REFERENCES node(id) ON DELETE RESTRICT,
    PRIMARY KEY (src, dst),
    CHECK (src <> dst)
) WITHOUT ROWID;

CREATE INDEX edge_dst ON edge(dst);

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

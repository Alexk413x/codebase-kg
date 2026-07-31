"""The constraints are the accuracy argument — prove they actually hold.

Each of these was previously a class of defect `kg_validate` reported *after*
the fact. Writing directly against the store, bypassing the writer's own
validation, shows the file itself rejects them.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from codebase_kg.schema import DDL


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(DDL)
    c.execute("INSERT INTO node (id, kind, description) VALUES ('a', 'K', 'One.')")
    c.execute("INSERT INTO node (id, kind, description) VALUES ('b', 'K', 'Two.')")
    yield c
    c.close()


def _node(conn: sqlite3.Connection, **cols: object) -> None:
    cols.setdefault("id", "n")
    cols.setdefault("kind", "K")
    keys = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    conn.execute(f"INSERT INTO node ({keys}) VALUES ({marks})", tuple(cols.values()))


# --- identity ----------------------------------------------------------------
def test_duplicate_node_id_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, id="a")


def test_empty_id_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, id="")


def test_empty_kind_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, kind="")


# --- edges -------------------------------------------------------------------
def test_dangling_edge_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'ghost')")


def test_self_edge_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'a')")


def test_duplicate_edge_is_impossible(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'b')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'b')")


def test_deleting_a_node_with_inbound_edges_is_refused(conn: sqlite3.Connection) -> None:
    # ON DELETE RESTRICT on `dst`: removing a node cannot silently strand the
    # edges that point at it.
    conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'b')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM node WHERE id = 'b'")


def test_deleting_a_node_cascades_its_own_edges_and_anchors(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO edge (src, dst) VALUES ('a', 'b')")
    conn.execute("INSERT INTO anchor (node_id, ord, path, base) VALUES ('a', 0, 'src/A.kt', 'A.kt')")
    conn.execute("DELETE FROM node WHERE id = 'a'")
    assert conn.execute("SELECT count(*) FROM edge").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM anchor").fetchone()[0] == 0


# --- anchors -----------------------------------------------------------------
def test_orphan_anchor_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO anchor (node_id, ord, path, base) VALUES ('ghost', 0, 'x.kt', 'x.kt')")


def test_line_number_anchor_is_impossible(conn: sqlite3.Connection) -> None:
    # SCHEMA.md §4.1 — line numbers are the #1 drift source, so the store
    # refuses them outright rather than trusting the author to remember.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base, symbol)"
            " VALUES ('a', 0, 'x.kt', 'x.kt', '42')"
        )


def test_symbol_anchor_is_accepted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO anchor (node_id, ord, path, base, symbol)"
        " VALUES ('a', 0, 'x.kt', 'x.kt', 'Foo')"
    )


def test_backslash_path_is_impossible(conn: sqlite3.Connection) -> None:
    # Paths are posix-normalized on the write path (models.Anchor); the store
    # refuses anything else so readers can compare `path` without normalizing.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base)"
            " VALUES ('a', 0, 'dir' || char(92) || 'A.kt', 'A.kt')"
        )


# --- description -------------------------------------------------------------
def test_over_length_description_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, description="x" * 241)


# --- parity (SCHEMA.md §8) ---------------------------------------------------
def test_unknown_parity_value_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, parity="probably-fine")


def test_matched_without_counterpart_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, parity="matched")


def test_divergent_without_divergence_line_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, parity="divergent", counterpart="../peer.db#x")


def test_only_with_counterpart_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, parity="android-only", counterpart="../peer.db#x")


def test_counterpart_without_parity_is_impossible(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, counterpart="../peer.db#x")


def test_divergence_without_divergent_parity_is_impossible(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _node(conn, parity="matched", counterpart="../peer.db#x", divergence="differs")


@pytest.mark.parametrize(
    "cols",
    [
        {"parity": None},
        {"parity": "matched", "counterpart": "../peer.db#x"},
        {"parity": "divergent", "counterpart": "../peer.db#x", "divergence": "differs"},
        {"parity": "android-only"},
        {"parity": "web-only"},
    ],
)
def test_every_legal_parity_shape_is_accepted(
    conn: sqlite3.Connection, cols: dict[str, object]
) -> None:
    _node(conn, **cols)

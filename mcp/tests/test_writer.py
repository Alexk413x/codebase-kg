"""The writer's guarantees: validation, atomicity, determinism.

The schema-level constraints these lean on are covered in test_schema.py; here
the concern is that the writer surfaces violations as named errors and never
leaves a half-written artifact behind.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.schema import APPLICATION_ID, SCHEMA_VERSION
from codebase_kg.writer import BuildError, build


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _nodes() -> list[Node]:
    return [
        Node(id="a", kind="K", description="First.", edges=["b"],
             anchors=[Anchor("src/A.kt", "A")]),
        Node(id="b", kind="K", description="Second."),
    ]


# --- happy path --------------------------------------------------------------
def test_build_reports_counts(tmp_path: Path, sample_meta: Meta) -> None:
    r = build(tmp_path / "g.db", sample_meta, _nodes())
    assert (r.nodes, r.edges, r.anchors) == (2, 1, 1)
    assert r.dropped_edges == []


def test_build_stamps_schema_version_and_application_id(
    tmp_path: Path, sample_meta: Meta
) -> None:
    db = tmp_path / "g.db"
    build(db, sample_meta, _nodes())
    conn = sqlite3.connect(db)
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        app_id = conn.execute("PRAGMA application_id").fetchone()
    finally:
        conn.close()
    assert int(version[0]) == SCHEMA_VERSION
    assert app_id[0] == APPLICATION_ID


def test_build_skips_empty_meta_values(tmp_path: Path) -> None:
    build(tmp_path / "g.db", Meta(codebase="x", root="src", generated="2026-07-30"), _nodes())
    conn = sqlite3.connect(tmp_path / "g.db")
    try:
        keys = {r[0] for r in conn.execute("SELECT key FROM meta")}
    finally:
        conn.close()
    assert "language" not in keys and "counterpart" not in keys


# --- determinism -------------------------------------------------------------
def test_rebuild_of_identical_input_is_byte_identical(
    tmp_path: Path, sample_meta: Meta
) -> None:
    # Keeps an unchanged regeneration out of the git diff entirely, and lets a
    # caller compare hashes to ask whether the committed artifact is current.
    db = tmp_path / "g.db"
    build(db, sample_meta, _nodes())
    first = _sha(db)
    build(db, sample_meta, _nodes())
    assert _sha(db) == first


def test_input_order_does_not_change_the_file(tmp_path: Path, sample_meta: Meta) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    build(a, sample_meta, _nodes())
    build(b, sample_meta, list(reversed(_nodes())))
    assert _sha(a) == _sha(b)


def test_changed_input_changes_the_file(tmp_path: Path, sample_meta: Meta) -> None:
    db = tmp_path / "g.db"
    build(db, sample_meta, _nodes())
    first = _sha(db)
    changed = _nodes()
    changed[0].description = "First, revised."
    build(db, sample_meta, changed)
    assert _sha(db) != first


# --- validation --------------------------------------------------------------
def test_duplicate_ids_rejected(tmp_path: Path, sample_meta: Meta) -> None:
    dupes = [Node(id="a", kind="K"), Node(id="a", kind="K")]
    with pytest.raises(BuildError, match="duplicate node ids: a"):
        build(tmp_path / "g.db", sample_meta, dupes)


def test_dangling_edge_rejected_by_default(tmp_path: Path, sample_meta: Meta) -> None:
    with pytest.raises(BuildError, match="dangling edge to 'ghost'"):
        build(tmp_path / "g.db", sample_meta, [Node(id="a", kind="K", edges=["ghost"])])


def test_dangling_edge_dropped_and_reported_under_drop_policy(
    tmp_path: Path, sample_meta: Meta
) -> None:
    r = build(
        tmp_path / "g.db", sample_meta, [Node(id="a", kind="K", edges=["ghost"])],
        dangling="drop",
    )
    assert r.dropped_edges == [("a", "ghost")] and r.edges == 0


def test_bad_description_names_the_node(tmp_path: Path, sample_meta: Meta) -> None:
    bad = [Node(id="a", kind="K", description="Wires it up (ACME-1).")]
    with pytest.raises(BuildError, match="node 'a'.*ticket refs"):
        build(tmp_path / "g.db", sample_meta, bad)


def test_missing_kind_rejected(tmp_path: Path, sample_meta: Meta) -> None:
    with pytest.raises(BuildError, match="node 'a'.*no kind"):
        build(tmp_path / "g.db", sample_meta, [Node(id="a", kind="  ")])


@pytest.mark.parametrize(
    "node, expect",
    [
        (Node(id="a", kind="K", parity="matched"), "requires a counterpart"),
        (Node(id="a", kind="K", parity="divergent", counterpart="../p.db#x"),
         "requires a divergence line"),
        (Node(id="a", kind="K", parity="ios-only", counterpart="../p.db#x"),
         "must not have a counterpart"),
        (Node(id="a", kind="K", counterpart="../p.db#x"), "counterpart set with no parity"),
        (Node(id="a", kind="K", parity="nonsense"), "unrecognized parity"),
        (Node(id="a", kind="K", anchors=[Anchor("x.kt", "42")]), "line number"),
    ],
)
def test_parity_and_anchor_contract_is_named_not_a_raw_check(
    tmp_path: Path, sample_meta: Meta, node: Node, expect: str
) -> None:
    # These used to reach SQLite and surface as a bare IntegrityError naming a
    # CHECK expression, with no indication of which node was at fault.
    with pytest.raises(BuildError, match=f"node 'a'.*{expect}"):
        build(tmp_path / "g.db", sample_meta, [node])


def test_empty_id_rejected(tmp_path: Path, sample_meta: Meta) -> None:
    with pytest.raises(BuildError, match="empty id"):
        build(tmp_path / "g.db", sample_meta, [Node(id="", kind="K")])


def test_self_edge_dropped(tmp_path: Path, sample_meta: Meta) -> None:
    r = build(tmp_path / "g.db", sample_meta, [Node(id="a", kind="K", edges=["a"])])
    assert r.edges == 0


def test_duplicate_edges_collapse(tmp_path: Path, sample_meta: Meta) -> None:
    nodes = [Node(id="a", kind="K", edges=["b", "b"]), Node(id="b", kind="K")]
    assert build(tmp_path / "g.db", sample_meta, nodes).edges == 1


# --- atomicity ---------------------------------------------------------------
def test_failed_build_leaves_the_existing_artifact_untouched(
    tmp_path: Path, sample_meta: Meta
) -> None:
    db = tmp_path / "g.db"
    build(db, sample_meta, _nodes())
    before = _sha(db)
    with pytest.raises(BuildError):
        build(db, sample_meta, [Node(id="a", kind="K", edges=["ghost"])])
    assert _sha(db) == before


def test_failed_build_leaves_no_temp_files(tmp_path: Path, sample_meta: Meta) -> None:
    with pytest.raises(BuildError):
        build(tmp_path / "g.db", sample_meta, [Node(id="", kind="K")])
    assert list(tmp_path.iterdir()) == []


def test_build_creates_missing_parent_directory(tmp_path: Path, sample_meta: Meta) -> None:
    db = tmp_path / "knowledge" / "code_graph.db"
    build(db, sample_meta, _nodes())
    assert db.is_file()

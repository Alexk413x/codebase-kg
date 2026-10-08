"""Upgrading an existing graph in place, without losing anything.

A repo that already migrated to v2 has real work in its graph. The upgrade must
carry every node, anchor and edge across verbatim and only *add* what v3 knows
how to answer — declared coverage and source baselines.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from codebase_kg import upgrade
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.schema import SCHEMA_VERSION
from codebase_kg.store import CodeGraph, StoreError
from codebase_kg.writer import build


def _v2_graph(tmp_path: Path) -> tuple[Path, Path]:
    """A graph built by the current writer, then rewritten to look like v2.

    Building a genuine v2 file would mean vendoring the old DDL; downgrading a
    current one exercises the same thing the upgrader must cope with — a missing
    `source` table and an older version stamp.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "Feed.kt").write_text("class Feed", encoding="utf-8")
    (src / "Saved.kt").write_text("class Saved", encoding="utf-8")

    db = tmp_path / "code_graph.db"
    nodes = [
        Node(
            id="feed",
            kind="Domain",
            description="Ranks the feed.",
            anchors=[Anchor("src/Feed.kt", "Feed")],
            edges=["saved"],
            section="DOMAIN",
        ),
        Node(
            id="saved",
            kind="Entity",
            description="A saved article.",
            anchors=[Anchor("src/Saved.kt", "Saved")],
        ),
    ]
    build(db, Meta(codebase="x", root="", generated="2026-07-30"), nodes)

    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE source")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    return tmp_path, db


def test_a_v2_graph_is_refused_by_the_store(tmp_path: Path) -> None:
    _, db = _v2_graph(tmp_path)
    with pytest.raises(StoreError, match="schema v2"):
        CodeGraph(db)


def test_upgrade_preserves_every_node_anchor_and_edge(tmp_path: Path) -> None:
    base, db = _v2_graph(tmp_path)
    assert upgrade.main([str(db), "--source-root", str(base)]) == 0

    g = CodeGraph(db)
    try:
        nodes = {n.id: n for n in g.all_nodes()}
        assert set(nodes) == {"feed", "saved"}
        assert nodes["feed"].edges == ["saved"]
        assert nodes["feed"].description == "Ranks the feed."
        assert nodes["feed"].section == "DOMAIN"
        assert [str(a) for a in nodes["feed"].anchors] == ["src/Feed.kt#Feed"]
        # ...and it gained what v3 adds.
        assert set(g.sources()) == {"src/Feed.kt", "src/Saved.kt"}
    finally:
        g.close()


def test_upgrade_records_the_declaration(tmp_path: Path) -> None:
    base, db = _v2_graph(tmp_path)
    assert (
        upgrade.main(
            [str(db), "--source-root", str(base), "--covers", "src/**/*.kt", "--exempt", "**/Gen*.kt"]
        )
        == 0
    )
    g = CodeGraph(db)
    try:
        assert g.meta.covers == ["src/**/*.kt"]
        assert g.meta.exempt == ["**/Gen*.kt"]
    finally:
        g.close()


def test_upgrading_an_already_current_graph_is_a_no_op(tmp_path: Path, capsys) -> None:
    base, db = _v2_graph(tmp_path)
    upgrade.main([str(db), "--source-root", str(base)])
    before = db.read_bytes()

    assert upgrade.main([str(db)]) == 0
    assert "already schema" in capsys.readouterr().out
    assert db.read_bytes() == before


def test_a_failed_upgrade_leaves_the_original_untouched(tmp_path: Path) -> None:
    base, db = _v2_graph(tmp_path)
    original = db.read_bytes()

    # A ticket reference is rejected by the writer's node contract but not by any
    # CHECK, so it is a violation that can exist in a file yet still fail the
    # rebuild — exactly the case where "nothing was written" has to hold.
    # (Length, by contrast, cannot even be poisoned: the CHECK refuses the UPDATE.)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE node SET description = ? WHERE id = 'feed'",
            ("Ranks the feed (ACME-433).",),
        )
        conn.commit()
    finally:
        conn.close()
    poisoned = db.read_bytes()

    assert upgrade.main([str(db), "--source-root", str(base)]) == 1
    assert db.read_bytes() == poisoned != original


def test_the_version_stamp_moves(tmp_path: Path) -> None:
    base, db = _v2_graph(tmp_path)
    upgrade.main([str(db), "--source-root", str(base)])
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    finally:
        conn.close()
    assert int(row[0]) == SCHEMA_VERSION

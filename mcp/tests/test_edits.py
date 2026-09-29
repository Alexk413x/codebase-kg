"""The write tools' guarantees: atomicity, validation, and an honest report.

The schema constraints are covered in test_schema.py and the whole-graph write
path in test_writer.py. What matters here is that a targeted edit either lands
completely or leaves the committed file byte-identical, that it is judged
against the graph it produces rather than against the constraints alone, and
that it says what it did in enough detail to review.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from codebase_kg import edits, tools
from codebase_kg.links import ExternalLink
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build, file_sha


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def graph(tmp_path: Path) -> Path:
    """A small repo-shaped graph with real source behind its anchors.

    `exempt` repeats `covers` on purpose: every file is in scope and none is
    required to have a node, so a test that deletes a node exercises the delete
    rather than tripping the coverage check -- which has its own test below.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.kt").write_text("class A { fun go() {} }\n", encoding="utf-8")
    (src / "B.kt").write_text("class B\n", encoding="utf-8")
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="test", root="src", generated="2026-08-05",
             covers=["**/*.kt"], exempt=["**/*.kt"]),
        [
            Node(id="a", kind="Service", description="Does the A thing.",
                 anchors=[Anchor("A.kt", "A")], edges=["b"]),
            Node(id="b", kind="Entity", description="Holds the B thing.",
                 anchors=[Anchor("B.kt", "B")]),
        ],
        source_root=src,
    )
    return db


def _node(db: Path, node_id: str) -> Node | None:
    g = CodeGraph(db)
    try:
        return g.node(node_id)
    finally:
        g.close()



def test_a_write_reads_the_source_tree_once_for_both_validations(
    graph: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source does not change between the before and after checks, so the
    walk and each anchored file's read and digest happen once per write."""
    reads: list[Path] = []
    walks: list[Path] = []
    read, walk = tools._read_source, tools.walk_sources
    monkeypatch.setattr(tools, "_read_source", lambda fp: (reads.append(fp), read(fp))[1])
    monkeypatch.setattr(
        tools, "walk_sources", lambda base, keep=(): (walks.append(base), walk(base, keep))[1]
    )
    result = edits.upsert_node(graph, [{"id": "a", "description": "Does the A thing, revised."}])
    assert result["written"] is True
    assert sorted(p.name for p in reads) == ["A.kt", "B.kt"]
    assert len(walks) == 1
# --- upsert ------------------------------------------------------------------
def test_an_upsert_changes_only_the_fields_it_names(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "a", "description": "Does the A thing, revised."}])
    a = _node(graph, "a")
    assert a is not None
    assert a.description == "Does the A thing, revised."
    assert a.kind == "Service" and [str(x) for x in a.anchors] == ["A.kt#A"]
    assert a.edges == ["b"]


def test_an_upsert_creates_a_node_that_did_not_exist(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "c", "kind": "Module", "description": "New."}])
    assert _node(graph, "c") is not None


def test_a_new_node_without_a_kind_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="needs a `kind`"):
        edits.upsert_node(graph, [{"id": "c", "description": "New."}])


def test_an_explicit_null_clears_the_parity_triple(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "a", "parity": "ios-only"}])
    edits.upsert_node(graph, [{"id": "a", "parity": None}])
    a = _node(graph, "a")
    assert a is not None and a.parity is None


def test_a_list_field_replaces_rather_than_appends(graph: Path) -> None:
    # Stated as a test because the alternative is defensible and the difference
    # is invisible in a response that only shows the new value.
    edits.upsert_node(graph, [{"id": "a", "anchors": ["B.kt#B"]}])
    a = _node(graph, "a")
    assert a is not None and [str(x) for x in a.anchors] == ["B.kt#B"]


def test_an_upsert_rewrites_the_search_index_for_that_node(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "a", "description": "Ranks the feed by freshness."}])
    g = CodeGraph(graph)
    try:
        assert [i for i, _ in g.search_ids("freshness", 5)] == ["a"]
    finally:
        g.close()


def test_two_new_nodes_may_point_at_each_other_in_one_call(graph: Path) -> None:
    # Only possible if every node row is written before any edge is.
    edits.upsert_node(
        graph,
        [
            {"id": "c", "kind": "K", "description": "C.", "edges": ["d"]},
            {"id": "d", "kind": "K", "description": "D.", "edges": ["c"]},
        ],
    )
    c, d = _node(graph, "c"), _node(graph, "d")
    assert c is not None and d is not None
    assert c.edges == ["d"] and d.edges == ["c"]


def test_the_same_node_twice_in_one_call_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="appears twice"):
        edits.upsert_node(graph, [{"id": "a", "section": "X"}, {"id": "a", "section": "Y"}])


def test_a_self_edge_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="edge to itself"):
        edits.upsert_node(graph, [{"id": "a", "edges": ["a"]}])


# --- atomicity ---------------------------------------------------------------
def test_a_batch_rejected_on_its_third_node_writes_none_of_the_first_two(
    graph: Path,
) -> None:
    before = _sha(graph)
    with pytest.raises(edits.EditError, match="node 'three'"):
        edits.upsert_node(
            graph,
            [
                {"id": "one", "kind": "K", "description": "One."},
                {"id": "two", "kind": "K", "description": "Two."},
                {"id": "three", "kind": "K", "description": "x" * 300},
                {"id": "four", "kind": "K", "description": "Four."},
            ],
        )
    assert _sha(graph) == before
    assert _node(graph, "one") is None


def test_a_failure_only_sql_can_see_still_leaves_the_file_byte_identical(
    graph: Path,
) -> None:
    # A dangling edge passes every pre-write check and fails on the foreign key
    # mid-transaction, which is the case a copy-then-swap has to survive.
    before = _sha(graph)
    with pytest.raises(edits.EditError, match="FOREIGN KEY"):
        edits.upsert_node(
            graph,
            [
                {"id": "one", "kind": "K", "description": "One."},
                {"id": "two", "kind": "K", "description": "Two.", "edges": ["ghost"]},
            ],
        )
    assert _sha(graph) == before


def test_a_rejected_edit_leaves_no_temp_file_beside_the_graph(graph: Path) -> None:
    with pytest.raises(edits.EditError):
        edits.upsert_node(graph, [{"id": "a", "description": "Fixed in ACME-431."}])
    assert [p.name for p in graph.parent.iterdir()] == [graph.name]


def test_an_edit_that_changes_nothing_does_not_rewrite_the_file(graph: Path) -> None:
    # A no-op write would still change the file's bytes and put a diff in front
    # of a reviewer for an edit that did not happen.
    before = _sha(graph)
    result = edits.upsert_node(graph, [{"id": "a", "description": "Does the A thing."}])
    assert result["written"] is False
    assert _sha(graph) == before


# --- validation, not merely constraints --------------------------------------
def test_an_edit_that_uncovers_a_declared_file_is_rolled_back(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.kt").write_text("class A\n", encoding="utf-8")
    (src / "B.kt").write_text("class B\n", encoding="utf-8")
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="t", root="src", covers=["**/*.kt"]),
        [Node(id="a", kind="K", description="A.", anchors=[Anchor("A.kt", "A")]),
         Node(id="b", kind="K", description="B.", anchors=[Anchor("B.kt", "B")])],
        source_root=src,
    )
    before = _sha(db)
    with pytest.raises(edits.EditError, match="coverage gap: A.kt"):
        edits.delete_node(db, ["a"], dry_run=False)
    assert _sha(db) == before


def test_a_graph_that_already_has_findings_can_still_be_edited(tmp_path: Path) -> None:
    # The test is "no NEW findings", never "clean" -- a graph mid-refactor
    # carries open findings, and refusing to touch it would strand it there.
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.kt").write_text("class Real\n", encoding="utf-8")
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="t", root="src"),
        [Node(id="a", kind="K", description="A.", anchors=[Anchor("gone.kt", "A")]),
         Node(id="b", kind="K", description="B.", anchors=[Anchor("real.kt", "Real")])],
        source_root=src,
    )
    result = edits.upsert_node(db, [{"id": "a", "description": "A, revised."}])
    assert result["written"] is True
    assert result["validation"]["ok_before"] is False


def test_a_broken_link_is_refused_when_the_peer_graph_is_present(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    build(knowledge / "peer.db", Meta(codebase="peer", root="src"),
          [Node(id="known", kind="K", description="Known.")])
    db = knowledge / "code_graph.db"
    build(db, Meta(codebase="t", root="src"), [Node(id="a", kind="K", description="A.")])
    before = _sha(db)
    with pytest.raises(edits.EditError, match="does not contain"):
        edits.add_link(db, "a", "peer.db#missing")
    assert _sha(db) == before
    assert edits.add_link(db, "a", "peer.db#known", "implements")["written"] is True


def test_an_absent_peer_graph_does_not_block_a_link(graph: Path) -> None:
    # GRAPH-LINKS.md §4: each graph must be usable alone, so an unverifiable
    # link is a warning. Blocking here would make the peer plugin mandatory.
    result = edits.add_link(graph, "a", "cartographer_graph.db#some-screen", "presented-by")
    assert result["written"] is True
    assert any("not in this repo" in w for w in result["validation"]["new_warnings"])


# --- deletes and their blast radius ------------------------------------------
def test_a_delete_previews_by_default_and_writes_nothing(graph: Path) -> None:
    before = _sha(graph)
    result = edits.delete_node(graph, ["b"])
    assert result["written"] is False and result["dry_run"] is True
    assert _sha(graph) == before
    assert _node(graph, "b") is not None


def test_the_preview_names_the_inbound_edges_that_block_the_delete(graph: Path) -> None:
    result = edits.delete_node(graph, ["b"])
    assert result["would_delete"]["inbound_edges"] == ["a -> b"]
    assert any("RESTRICT" in n for n in result["notes"])


def test_an_inbound_edge_blocks_a_delete_until_it_is_named(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="RESTRICT"):
        edits.delete_node(graph, ["b"], dry_run=False)
    result = edits.delete_node(graph, ["b"], dry_run=False, cascade_inbound=True)
    assert result["written"] is True
    assert _node(graph, "b") is None
    assert _node(graph, "a") is not None and _node(graph, "a").edges == []  # type: ignore[union-attr]


def test_a_delete_reports_the_anchors_and_edges_it_took_with_it(graph: Path) -> None:
    result = edits.delete_node(graph, ["a"], dry_run=False)
    deleted = next(c for c in result["changes"] if c["table"] == "node")
    fields = {f["field"]: f["before"] for f in deleted["fields"]}
    assert fields["anchors"] == ["a: A.kt#A"]
    assert fields["outbound_edges"] == ["a -> b"]


def test_deleting_a_node_removes_it_from_the_search_index(graph: Path) -> None:
    edits.delete_node(graph, ["a"], dry_run=False)
    conn = sqlite3.connect(f"file:{graph.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT node_id FROM node_fts WHERE node_id = 'a'").fetchall()
    finally:
        conn.close()
    # A virtual table has no foreign key, so nothing cascades here -- an orphaned
    # FTS row would keep answering kg_search for a node that is gone.
    assert rows == []


def test_two_nodes_that_point_at_each_other_delete_together(graph: Path) -> None:
    edits.upsert_node(
        graph,
        [
            {"id": "c", "kind": "K", "description": "C.", "edges": ["d"]},
            {"id": "d", "kind": "K", "description": "D.", "edges": ["c"]},
        ],
    )
    assert edits.delete_node(graph, ["c", "d"], dry_run=False)["written"] is True
    assert _node(graph, "c") is None and _node(graph, "d") is None


def test_deleting_a_node_that_is_not_there_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="no such node"):
        edits.delete_node(graph, ["ghost"], dry_run=False)


# --- links -------------------------------------------------------------------
def test_a_link_target_that_is_not_db_hash_id_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="not '<db-file>#<node-id>'"):
        edits.add_link(graph, "a", "cartographer_graph.db")


def test_the_same_target_cannot_carry_two_kinds_on_one_node(graph: Path) -> None:
    edits.add_link(graph, "a", "peer.db#x", "implements")
    with pytest.raises(edits.EditError, match="already links"):
        edits.add_link(graph, "a", "peer.db#x", "tests")


def test_adding_a_link_that_is_already_there_writes_nothing(graph: Path) -> None:
    edits.add_link(graph, "a", "peer.db#x", "implements")
    before = _sha(graph)
    assert edits.add_link(graph, "a", "peer.db#x", "implements")["written"] is False
    assert _sha(graph) == before


def test_removing_a_link_leaves_the_node_and_its_anchors_alone(graph: Path) -> None:
    edits.add_link(graph, "a", "peer.db#x", "implements")
    edits.remove_link(graph, "a", "peer.db#x")
    a = _node(graph, "a")
    assert a is not None and a.links == []
    assert [str(x) for x in a.anchors] == ["A.kt#A"] and a.edges == ["b"]


def test_removing_a_link_that_is_not_there_is_refused(graph: Path) -> None:
    with pytest.raises(edits.EditError, match="does not link to"):
        edits.remove_link(graph, "a", "peer.db#nope")


def test_a_link_can_be_added_to_a_graph_written_before_the_table_existed(
    tmp_path: Path,
) -> None:
    # `external_link` was added without a schema bump, so a committed v3 artifact
    # may not have the table at all. Adopting the mechanism must not require a
    # rebuild, or "additive" bought nothing.
    db = tmp_path / "knowledge" / "code_graph.db"
    build(db, Meta(codebase="t", root="src"), [Node(id="a", kind="K", description="A.")])
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE external_link")
        conn.commit()
    finally:
        conn.close()
    assert edits.add_link(db, "a", "peer.db#x", "implements")["written"] is True
    a = _node(db, "a")
    assert a is not None and a.links == [ExternalLink(target="peer.db#x", kind="implements")]


# --- source baselines ----------------------------------------------------------
def _sources(db: Path) -> dict[str, str]:
    g = CodeGraph(db)
    try:
        return g.sources()
    finally:
        g.close()


def _changed_nodes(db: Path) -> set[str]:
    g = CodeGraph(db)
    try:
        report = tools.kg_validate(g)
    finally:
        g.close()
    return {c["node"] for c in report["changed_since_built"]["anchors"]}


def _drift(db: Path, name: str) -> None:
    src = db.parent.parent / "src" / name
    src.write_text(src.read_text(encoding="utf-8") + "// rewritten\n", encoding="utf-8")


def _source_changes(result: dict[str, object]) -> dict[str, str]:
    changes = result["changes"]
    assert isinstance(changes, list)
    return {c["key"]: c["action"] for c in changes if c["table"] == "source"}


def test_an_upsert_without_rebaseline_leaves_a_drifted_baseline_alone(graph: Path) -> None:
    before = _sources(graph)
    _drift(graph, "A.kt")
    result = edits.upsert_node(graph, [{"id": "a", "description": "Does the A thing, revised."}])
    assert result["written"] is True
    assert _source_changes(result) == {}
    assert _sources(graph)["A.kt"] == before["A.kt"]
    assert _changed_nodes(graph) == {"a"}


def test_rebaseline_clears_changed_since_built(graph: Path) -> None:
    _drift(graph, "A.kt")
    result = edits.upsert_node(
        graph, [{"id": "a", "description": "Does the A thing, rechecked.", "rebaseline": True}]
    )
    assert _source_changes(result) == {"A.kt": "updated"}
    assert _sources(graph)["A.kt"] == file_sha(graph.parent.parent / "src" / "A.kt")
    assert _changed_nodes(graph) == set()


def test_rebaseline_alone_is_enough_to_write(graph: Path) -> None:
    _drift(graph, "A.kt")
    result = edits.upsert_node(graph, [{"id": "a", "rebaseline": True}])
    assert result["written"] is True
    assert _changed_nodes(graph) == set()


def test_rebaseline_on_an_unchanged_file_writes_nothing(graph: Path) -> None:
    before = _sha(graph)
    result = edits.upsert_node(graph, [{"id": "a", "rebaseline": True}])
    assert result["written"] is False
    assert _sha(graph) == before


def test_rebaselining_a_shared_file_clears_it_for_every_node_on_it(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "c", "kind": "K", "description": "C.", "anchors": ["A.kt#A"]}])
    _drift(graph, "A.kt")
    assert _changed_nodes(graph) == {"a", "c"}
    edits.upsert_node(graph, [{"id": "a", "rebaseline": True}])
    assert _changed_nodes(graph) == set()


def test_a_new_anchor_path_gets_a_baseline(graph: Path) -> None:
    c_kt = graph.parent.parent / "src" / "C.kt"
    c_kt.write_text("class C\n", encoding="utf-8")
    result = edits.upsert_node(graph, [{"id": "a", "anchors": ["A.kt#A", "C.kt#C"]}])
    assert _source_changes(result) == {"C.kt": "created"}
    assert _sources(graph)["C.kt"] == file_sha(c_kt)


def test_a_path_nothing_anchors_any_more_loses_its_baseline(graph: Path) -> None:
    result = edits.upsert_node(graph, [{"id": "b", "anchors": ["A.kt#A"]}])
    assert _source_changes(result) == {"B.kt": "deleted"}
    assert "B.kt" not in _sources(graph)


def test_a_path_another_node_still_anchors_keeps_its_baseline(graph: Path) -> None:
    edits.upsert_node(graph, [{"id": "c", "kind": "K", "description": "C.", "anchors": ["B.kt#B"]}])
    before = _sources(graph)["B.kt"]
    result = edits.upsert_node(graph, [{"id": "b", "anchors": ["A.kt#A"]}])
    assert _source_changes(result) == {}
    assert _sources(graph)["B.kt"] == before


def test_a_delete_drops_the_baseline_of_a_file_it_orphans(graph: Path) -> None:
    preview = edits.delete_node(graph, ["b"])
    assert preview["would_delete"]["baselines"] == ["B.kt"]
    result = edits.delete_node(graph, ["b"], dry_run=False, cascade_inbound=True)
    assert _source_changes(result) == {"B.kt": "deleted"}
    assert _sources(graph) == {"A.kt": file_sha(graph.parent.parent / "src" / "A.kt")}


@pytest.mark.parametrize("value", ["true", 1, None, "yes"])
def test_a_rebaseline_that_is_not_a_boolean_is_refused(graph: Path, value: object) -> None:
    before = _sha(graph)
    with pytest.raises(edits.EditError, match="`rebaseline` must be true or false"):
        edits.upsert_node(graph, [{"id": "a", "description": "Revised.", "rebaseline": value}])
    assert _sha(graph) == before


def test_rebaselining_a_file_that_cannot_be_read_is_refused(graph: Path) -> None:
    (graph.parent.parent / "src" / "A.kt").unlink()
    before = _sha(graph)
    with pytest.raises(edits.EditError, match="cannot re-baseline A.kt"):
        edits.upsert_node(graph, [{"id": "a", "rebaseline": True}])
    assert _sha(graph) == before


# --- the report --------------------------------------------------------------
def test_every_change_reports_the_field_before_and_after(graph: Path) -> None:
    result = edits.upsert_node(graph, [{"id": "a", "description": "Revised."}])
    change = next(c for c in result["changes"] if c["table"] == "node")
    assert change["action"] == "updated"
    assert change["fields"] == [
        {"field": "description", "before": "Does the A thing.", "after": "Revised."}
    ]


def test_a_missing_graph_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(edits.EditError, match="codebase-kg:build"):
        edits.upsert_node(tmp_path / "nope.db", [{"id": "a", "kind": "K"}])

"""The `reference` table: where a platform fact a node depends on is documented.

`fixtures/v3/code_graph.db` is a real schema-v3 artifact, written by the v3
writer before `reference` existed. It is never regenerated: the tests below need
a file the current writer did not produce.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from codebase_kg import codec, edits, tools, upgrade
from codebase_kg.models import Anchor, Meta, Node, Reference, ReferenceFormatError
from codebase_kg.schema import SCHEMA_VERSION
from codebase_kg.store import CodeGraph
from codebase_kg.writer import BuildError, build

V3_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v3"

DOCS = "https://developer.android.com/reference/android/view/TouchDelegate"
RFC = "https://www.rfc-editor.org/rfc/rfc9110"


def _nodes() -> list[Node]:
    return [
        Node(
            id="feed", kind="Domain", description="Ranks the feed.",
            anchors=[Anchor("src/Feed.kt", "Feed"), Anchor("src/Feed.kt", "Feed.rank")],
            edges=["saved"],
            references=[
                Reference(url=DOCS, kind="platform-api", title="TouchDelegate",
                          path="src/Feed.kt", symbol="Feed.rank"),
                Reference(url=RFC, kind="rfc"),
                Reference(url=DOCS, path="src/Feed.kt"),
            ],
        ),
        Node(id="saved", kind="Entity", description="A saved article.",
             anchors=[Anchor("src/Saved.kt", "Saved")]),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose anchors resolve, so `kg_validate` starts clean."""
    shutil.copytree(V3_FIXTURE / "src", tmp_path / "src")
    (tmp_path / "knowledge").mkdir()
    return tmp_path


@pytest.fixture
def db(repo: Path) -> Path:
    path = repo / "knowledge" / "code_graph.db"
    build(path, Meta(codebase="x", root="", generated="2026-09-20"), _nodes(),
          source_root=repo)
    return path


@pytest.fixture
def v3_db(repo: Path) -> Path:
    path = repo / "knowledge" / "code_graph.db"
    shutil.copyfile(V3_FIXTURE / "code_graph.db", path)
    return path


def _meta_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _export(path: Path) -> dict:
    g = CodeGraph(path)
    try:
        return codec.to_dict(g.meta, g.all_nodes(), g.sources())
    finally:
        g.close()


def _sql(path: Path, statement: str, args: tuple = ()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


# --- the table ---------------------------------------------------------------
def test_a_build_writes_the_table_and_stamps_v4(db: Path) -> None:
    assert "reference" in _tables(db)
    assert _meta_version(db) == SCHEMA_VERSION == 4


@pytest.mark.parametrize(
    "row",
    [
        ("feed", 9, "", None, None),                          # empty url
        ("ghost", 9, DOCS, None, None),                       # no such node
        ("feed", 9, DOCS, None, "Feed"),                      # symbol without a path
        ("feed", 9, DOCS, "src\\Feed.kt", None),              # backslash path
        ("feed", 9, DOCS, "src/Feed.kt", "42"),               # a line number
        ("feed", 9, DOCS, "", None),                          # empty path
    ],
)
def test_the_constraints_refuse_a_malformed_row(db: Path, row: tuple) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _sql(db, "INSERT INTO reference (node_id, ord, url, path, symbol) VALUES (?,?,?,?,?)", row)


def test_references_cascade_with_their_node(db: Path) -> None:
    _sql(db, "DELETE FROM edge")
    _sql(db, "DELETE FROM node WHERE id = 'feed'")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM reference").fetchone()[0] == 0
    finally:
        conn.close()


# --- the model ---------------------------------------------------------------
def test_parse_accepts_a_bare_url_and_an_object() -> None:
    assert Reference.parse(DOCS) == Reference(url=DOCS)
    assert Reference.parse({"url": DOCS, "path": "a\\b.kt", "symbol": " X "}) == Reference(
        url=DOCS, path="a/b.kt", symbol="X"
    )


@pytest.mark.parametrize("raw", [{}, {"url": " "}, {"url": 3}, 7, {"url": DOCS, "path": 1}])
def test_parse_refuses_what_is_not_a_reference(raw: object) -> None:
    with pytest.raises(ReferenceFormatError):
        Reference.parse(raw)


def test_the_narrowing_rule() -> None:
    anchors = [Anchor("a.kt", "A"), Anchor("manifest.xml")]
    assert Reference(url=DOCS).problem(anchors) is None
    assert Reference(url=DOCS, path="a.kt").problem(anchors) is None
    assert Reference(url=DOCS, path="a.kt", symbol="A").problem(anchors) is None
    assert Reference(url=DOCS, path="manifest.xml").problem(anchors) is None
    assert "not one of this node's anchors" in (
        Reference(url=DOCS, path="b.kt").problem(anchors) or "")
    assert "not one of this node's anchors" in (
        Reference(url=DOCS, path="a.kt", symbol="B").problem(anchors) or "")
    assert "without a path" in (Reference(url=DOCS, symbol="A").problem(anchors) or "")


# --- round trip --------------------------------------------------------------
def test_the_json_round_trip_preserves_references_in_order() -> None:
    meta = Meta(codebase="x", root="", generated="2026-09-20")
    _, out, _ = codec.from_dict(codec.to_dict(meta, _nodes()))
    assert out == _nodes()
    assert [r.url for r in out[0].references] == [DOCS, RFC, DOCS]


def test_the_store_returns_what_was_built(db: Path) -> None:
    g = CodeGraph(db)
    try:
        assert g.all_nodes() == _nodes()
        feed = g.node("feed")
        assert feed is not None and feed.references == _nodes()[0].references
    finally:
        g.close()


def test_export_then_build_is_byte_identical_with_references(db: Path, tmp_path: Path) -> None:
    meta, nodes, sources = codec.from_dict(_export(db))
    assert any(n.references for n in nodes)
    rebuilt = tmp_path / "rebuilt.db"
    build(rebuilt, meta, nodes, sources=sources)
    assert rebuilt.read_bytes() == db.read_bytes()


def test_a_node_with_no_references_is_unchanged(db: Path) -> None:
    doc = _export(db)
    saved = next(n for n in doc["nodes"] if n["id"] == "saved")
    assert "references" not in saved
    g = CodeGraph(db)
    try:
        assert "references" not in tools.kg_node(g, "saved")
    finally:
        g.close()


def test_a_bad_reference_names_its_node() -> None:
    doc = {"nodes": [{"id": "a", "kind": "K", "references": [{"title": "no url"}]}]}
    with pytest.raises(codec.DecodeError, match="node 'a'.*no `url`"):
        codec.from_dict(doc)


# --- a v3 graph --------------------------------------------------------------
def test_the_fixture_really_is_v3() -> None:
    db = V3_FIXTURE / "code_graph.db"
    assert _meta_version(db) == 3
    assert "reference" not in _tables(db)


def test_a_v3_graph_still_opens_and_reads_as_no_references(v3_db: Path) -> None:
    g = CodeGraph(v3_db)
    try:
        nodes = {n.id: n for n in g.all_nodes()}
        assert set(nodes) == {"feed", "saved"}
        assert all(n.references == [] for n in nodes.values())
        assert g.references() == []
        assert tools.kg_find_by_reference(g)["count"] == 0
        report = tools.kg_validate(g)
        assert report["reference_issues"] == []
        assert report["anchor_issues"] == []
    finally:
        g.close()


def test_upgrade_migrates_v3_to_v4_and_loses_nothing(v3_db: Path, repo: Path) -> None:
    before = _export(v3_db)
    assert upgrade.main([str(v3_db), "--source-root", str(repo)]) == 0

    assert _meta_version(v3_db) == 4
    assert "reference" in _tables(v3_db)
    after = _export(v3_db)
    assert after == before
    feed = next(n for n in after["nodes"] if n["id"] == "feed")
    assert feed["external_links"] == [
        {"target": "cartographer_graph.db#feed_screen", "kind": "presented-by"}
    ]
    assert set(after["sources"]) == {"src/Feed.kt", "src/Saved.kt"}
    assert after["extra"] == {"team": "mobile"}
    assert after["covers"] == ["src/**/*.kt"]


def test_the_migrated_graph_is_what_a_fresh_build_would_be(
    v3_db: Path, repo: Path, tmp_path: Path
) -> None:
    upgrade.main([str(v3_db), "--source-root", str(repo)])
    meta, nodes, sources = codec.from_dict(_export(v3_db))
    fresh = tmp_path / "fresh.db"
    build(fresh, meta, nodes, sources=sources)
    assert fresh.read_bytes() == v3_db.read_bytes()


def test_upgrade_carries_references_through_a_forced_rebuild(db: Path, repo: Path) -> None:
    assert upgrade.main([str(db), "--source-root", str(repo), "--force"]) == 0
    g = CodeGraph(db)
    try:
        assert g.all_nodes() == _nodes()
    finally:
        g.close()


def test_the_first_reference_written_to_a_v3_graph_upgrades_it_in_place(v3_db: Path) -> None:
    out = edits.add_reference(v3_db, "feed", DOCS, kind="platform-api",
                              ref_path="src/Feed.kt", symbol="Feed.rank")
    assert out["written"]
    assert _meta_version(v3_db) == 4
    assert {"reference", "meta"} <= {c["table"] for c in out["changes"]}
    g = CodeGraph(v3_db)
    try:
        feed = g.node("feed")
        assert feed is not None
        assert feed.references == [
            Reference(url=DOCS, kind="platform-api", path="src/Feed.kt", symbol="Feed.rank")
        ]
    finally:
        g.close()


def test_an_edit_that_adds_no_reference_leaves_a_v3_graph_at_v3(v3_db: Path) -> None:
    edits.upsert_node(v3_db, [{"id": "saved", "description": "A bookmarked article."}])
    assert _meta_version(v3_db) == 3
    assert "reference" not in _tables(v3_db)


# --- kg_validate -------------------------------------------------------------
def _validate(path: Path) -> dict:
    g = CodeGraph(path)
    try:
        return tools.kg_validate(g)
    finally:
        g.close()


def test_validate_is_quiet_when_every_narrowing_is_an_anchor(db: Path) -> None:
    report = _validate(db)
    assert report["reference_issues"] == []
    assert report["ok"]


@pytest.mark.parametrize(
    ("path", "symbol"),
    [
        ("src/Gone.kt", None),              # a path no anchor has
        ("src/Feed.kt", "Feed.vanished"),   # the right file, a symbol no anchor has
        ("src/Saved.kt", "Saved"),          # a real anchor, but another node's
    ],
)
def test_validate_reports_a_narrowing_that_is_not_the_nodes_own_anchor(
    db: Path, path: str, symbol: str | None
) -> None:
    _sql(db, "INSERT INTO reference (node_id, ord, url, path, symbol) VALUES ('feed',9,?,?,?)",
         (RFC, path, symbol))
    report = _validate(db)
    assert not report["ok"]
    [issue] = report["reference_issues"]
    assert issue["node"] == "feed" and issue["url"] == RFC
    assert issue["narrows_to"] == (f"{path}#{symbol}" if symbol else path)
    assert "not one of this node's anchors" in issue["issue"]


def test_validate_reports_a_reference_orphaned_by_a_moved_anchor(db: Path) -> None:
    # The rot this check exists for: the anchor moves, the reference does not.
    _sql(db, "UPDATE anchor SET symbol = 'Feed.score' WHERE symbol = 'Feed.rank'")
    issues = _validate(db)["reference_issues"]
    assert [i["narrows_to"] for i in issues] == ["src/Feed.kt#Feed.rank"]


# --- writes ------------------------------------------------------------------
def test_the_writer_refuses_a_narrowing_that_is_not_an_anchor(tmp_path: Path) -> None:
    nodes = [Node(id="a", kind="K", anchors=[Anchor("a.kt", "A")],
                  references=[Reference(url=DOCS, path="a.kt", symbol="B")])]
    with pytest.raises(BuildError, match="node 'a'.*not one of this node's anchors"):
        build(tmp_path / "g.db", Meta(codebase="x"), nodes)
    assert not (tmp_path / "g.db").exists()


def test_the_writer_refuses_the_same_reference_twice(tmp_path: Path) -> None:
    nodes = [Node(id="a", kind="K", references=[Reference(url=DOCS), Reference(url=DOCS)])]
    with pytest.raises(BuildError, match="listed twice"):
        build(tmp_path / "g.db", Meta(codebase="x"), nodes)


def test_add_reference_refuses_a_narrowing_that_is_not_an_anchor(db: Path) -> None:
    before = db.read_bytes()
    with pytest.raises(edits.EditError, match="not one of this node's anchors"):
        edits.add_reference(db, "saved", DOCS, ref_path="src/Feed.kt")
    assert db.read_bytes() == before


def test_an_upsert_cannot_remove_an_anchor_a_reference_narrows_to(db: Path) -> None:
    before = db.read_bytes()
    with pytest.raises(edits.EditError, match="src/Feed.kt#Feed.rank"):
        edits.upsert_node(db, [{"id": "feed", "anchors": ["src/Feed.kt#Feed"]}])
    assert db.read_bytes() == before


def test_add_then_remove_a_reference(db: Path) -> None:
    out = edits.add_reference(db, "saved", RFC, kind="rfc", title="HTTP Semantics")
    assert out["written"]
    assert edits.add_reference(db, "saved", RFC, kind="rfc", title="HTTP Semantics")[
        "written"] is False

    g = CodeGraph(db)
    try:
        assert tools.kg_node(g, "saved")["references"] == [
            {"url": RFC, "kind": "rfc", "title": "HTTP Semantics"}
        ]
        assert [h[0] for h in g.search_ids("semantics", 5)] == ["saved"]
    finally:
        g.close()

    assert edits.remove_reference(db, "saved", RFC)["written"]
    with pytest.raises(edits.EditError, match="has no reference"):
        edits.remove_reference(db, "saved", RFC)


def test_remove_reference_can_pick_one_narrowing(db: Path) -> None:
    edits.remove_reference(db, "feed", DOCS, ref_path="src/Feed.kt", symbol="Feed.rank")
    g = CodeGraph(db)
    try:
        feed = g.node("feed")
        assert feed is not None
        assert feed.references == [Reference(url=RFC, kind="rfc"),
                                   Reference(url=DOCS, path="src/Feed.kt")]
    finally:
        g.close()


def test_upsert_replaces_the_reference_list(db: Path) -> None:
    edits.upsert_node(db, [{"id": "feed", "references": [RFC]}])
    g = CodeGraph(db)
    try:
        feed = g.node("feed")
        assert feed is not None and feed.references == [Reference(url=RFC)]
    finally:
        g.close()


def test_a_delete_preview_lists_the_references_that_go(db: Path) -> None:
    out = edits.delete_node(db, ["feed"])
    assert out["would_delete"]["references"] == [
        f"feed -> {DOCS}", f"feed -> {RFC}", f"feed -> {DOCS}"
    ]


# --- the reverse lookup ------------------------------------------------------
def test_find_by_reference_matches_a_url_substring_and_a_kind(db: Path) -> None:
    g = CodeGraph(db)
    try:
        hits = tools.kg_find_by_reference(g, "developer.android.com/reference/android/view")
        assert hits["count"] == 2
        assert hits["references"][0] == {
            "node": "feed", "node_kind": "Domain", "url": DOCS, "kind": "platform-api",
            "title": "TouchDelegate", "path": "src/Feed.kt", "symbol": "Feed.rank",
        }
        assert tools.kg_find_by_reference(g, "touchdelegate")["count"] == 2
        assert [r["url"] for r in tools.kg_find_by_reference(g, kind="rfc")["references"]] == [RFC]
        assert tools.kg_find_by_reference(g, "nowhere.example")["count"] == 0
    finally:
        g.close()

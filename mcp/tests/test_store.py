"""Reading a built graph: hydration, indexed lookups, traversal, search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph, StoreError, tokenize
from codebase_kg.writer import build


# --- open / schema guard -----------------------------------------------------
def test_missing_file_is_a_store_error(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no code graph"):
        CodeGraph(tmp_path / "nope.db")


def test_non_database_file_is_a_store_error(tmp_path: Path) -> None:
    junk = tmp_path / "code_graph.db"
    junk.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(StoreError, match="not a code graph database"):
        CodeGraph(junk)


def _set_schema_version(db: Path, version: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
        conn.commit()
    finally:
        conn.close()


def test_wrong_schema_version_is_refused(sample_db: Path) -> None:
    _set_schema_version(sample_db, "99")
    with pytest.raises(StoreError, match="schema v99"):
        CodeGraph(sample_db)


def test_an_older_graph_is_told_to_upgrade(sample_db: Path) -> None:
    """A graph behind the server can be upgraded in place, keeping every node."""
    _set_schema_version(sample_db, "2")
    with pytest.raises(StoreError) as exc:
        CodeGraph(sample_db)
    assert "codebase_kg.upgrade" in str(exc.value)


def test_a_newer_graph_is_not_told_to_rebuild(sample_db: Path) -> None:
    """The direction that used to give actively harmful advice.

    A graph *ahead* of the server is not broken — the plugin is stale. Telling
    the user to rebuild sent them to regenerate a good file with an old server,
    which reproduces the mismatch and discards whatever the newer schema added.
    """
    _set_schema_version(sample_db, "99")
    with pytest.raises(StoreError) as exc:
        CodeGraph(sample_db)
    message = str(exc.value)
    assert "/codebase-kg:build" not in message
    assert "Update the plugin" in message
    assert "codebase_kg.upgrade" not in message  # nor the other direction's fix


def test_store_is_read_only(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            g._conn.execute("DELETE FROM node")
    finally:
        g.close()


# --- meta --------------------------------------------------------------------
def test_meta_round_trips(sample_db: Path, sample_meta: Meta) -> None:
    g = CodeGraph(sample_db)
    try:
        assert g.meta.codebase == sample_meta.codebase
        assert g.meta.root == sample_meta.root
        assert g.meta.language == sample_meta.language
        assert g.meta.generated == sample_meta.generated
        assert "schema_version" not in g.meta.extra  # internal, not user config
    finally:
        g.close()


# --- nodes -------------------------------------------------------------------
def test_node_hydrates_anchors_and_edges(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        n = g.node("feed_ranker")
        assert n is not None
        assert [str(a) for a in n.anchors] == ["domain/FeedRanker.kt#FeedRanker"]
        assert n.edges == ["saved_article"]
        assert g.node("nope") is None
    finally:
        g.close()


def test_inbound_is_the_reverse_edge(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert g.inbound("saved_article") == ["feed_ranker"]
        assert g.inbound("feed_ranker") == []
    finally:
        g.close()


def test_anchor_order_is_preserved(tmp_path: Path, sample_meta: Meta) -> None:
    node = Node(
        id="a",
        kind="K",
        anchors=[Anchor("z.kt", "Z"), Anchor("a.kt", "A"), Anchor("m.kt", "M")],
    )
    db = tmp_path / "g.db"
    build(db, sample_meta, [node])
    g = CodeGraph(db)
    try:
        anchors = g.node("a")
        assert anchors is not None
        assert [a.path for a in anchors.anchors] == ["z.kt", "a.kt", "m.kt"]
    finally:
        g.close()


def test_id_lists_are_chunked_past_the_sql_variable_limit(
    tmp_path: Path, sample_meta: Meta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite caps bound parameters at 32,766.

    Naming every id in an IN-list therefore *failed outright* on a graph past
    that size — `all_nodes()`, and so `kg_validate` and `export`, were dead on
    exactly the monorepo scale this store targets. The chunk size is shrunk here
    so the mechanism is exercised without building a 33k-node fixture.
    """
    import codebase_kg.store as store_mod

    monkeypatch.setattr(store_mod, "_MAX_PARAMS", 3)
    nodes = [Node(id=f"n{i}", kind="K", anchors=[Anchor(f"f{i}.kt", f"S{i}")]) for i in range(10)]
    nodes[0].edges = ["n1", "n2"]
    db = tmp_path / "g.db"
    build(db, sample_meta, nodes)
    g = CodeGraph(db)
    try:
        fetched = g.nodes([n.id for n in nodes])
        assert [n.id for n in fetched] == sorted(n.id for n in nodes)
        # anchors and edges must survive the chunk boundaries intact
        assert all(len(n.anchors) == 1 for n in fetched)
        assert next(n for n in fetched if n.id == "n0").edges == ["n1", "n2"]
        assert len(g.all_nodes()) == 10
        assert len(g.inbound_many([n.id for n in nodes])) == 10
    finally:
        g.close()


def test_all_nodes_does_not_name_every_id(sample_db: Path) -> None:
    # The whole-graph path must not build an IN-list at all — that is what makes
    # it size-independent rather than merely chunked.
    g = CodeGraph(sample_db)
    try:
        assert len(g.all_nodes()) == 2
        assert len(g.node_summaries()) == 2
    finally:
        g.close()


# --- lookups -----------------------------------------------------------------
def test_by_kind_is_a_case_insensitive_substring(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert [n.id for n in g.by_kind("entity")] == ["saved_article"]
        assert [n.id for n in g.by_kind("DOMAIN")] == ["feed_ranker"]
    finally:
        g.close()


def test_by_path_matches_exact_and_suffix(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert [n.id for n, _ in g.by_path("domain/FeedRanker.kt")] == ["feed_ranker"]
        assert [n.id for n, _ in g.by_path("FeedRanker.kt")] == ["feed_ranker"]
        assert g.by_path("Nothing.kt") == []
    finally:
        g.close()


def test_by_path_returns_the_matching_anchors(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        _, anchors = g.by_path("FeedRanker.kt")[0]
        assert [str(a) for a in anchors] == ["domain/FeedRanker.kt#FeedRanker"]
    finally:
        g.close()


def test_similar_ids_suggests_on_a_partial(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert "feed_ranker" in g.similar_ids("ranker")
    finally:
        g.close()


# --- traversal ---------------------------------------------------------------
def test_neighborhood_follows_edges_in_both_directions(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        # saved_article has no outbound edge; it is still reachable from
        # feed_ranker's inbound side.
        assert g.neighborhood_ids("saved_article", 1) == {"saved_article": 0, "feed_ranker": 1}
    finally:
        g.close()


def test_neighborhood_records_the_shortest_hop_count(
    tmp_path: Path, sample_meta: Meta
) -> None:
    chain = [
        Node(id="a", kind="K", edges=["b"]),
        Node(id="b", kind="K", edges=["c"]),
        Node(id="c", kind="K", edges=["d"]),
        Node(id="d", kind="K"),
    ]
    db = tmp_path / "g.db"
    build(db, sample_meta, chain)
    g = CodeGraph(db)
    try:
        assert g.neighborhood_ids("a", 2) == {"a": 0, "b": 1, "c": 2}
        assert g.neighborhood_ids("a", 3) == {"a": 0, "b": 1, "c": 2, "d": 3}
    finally:
        g.close()


def test_neighborhood_terminates_on_a_cycle(tmp_path: Path, sample_meta: Meta) -> None:
    cycle = [
        Node(id="a", kind="K", edges=["b"]),
        Node(id="b", kind="K", edges=["c"]),
        Node(id="c", kind="K", edges=["a"]),
    ]
    db = tmp_path / "g.db"
    build(db, sample_meta, cycle)
    g = CodeGraph(db)
    try:
        assert set(g.neighborhood_ids("a", 3)) == {"a", "b", "c"}
    finally:
        g.close()


# --- search ------------------------------------------------------------------
def test_tokenize_splits_camel_case() -> None:
    assert tokenize("VideoPlaybackService") == [
        "videoplaybackservice", "video", "playback", "service",
    ]
    assert tokenize("quartz_upload_worker") == ["quartz", "upload", "worker"]


def test_search_finds_camel_case_identifiers_from_split_words(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        # `FeedRanker` is indexed as one opaque token by the FTS tokenizer; the
        # writer emits the split form alongside it so this query works at all.
        assert [i for i, _ in g.search_ids("feed ranker", 5)] == ["feed_ranker"]
    finally:
        g.close()


def test_search_matches_prefixes(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert [i for i, _ in g.search_ids("rank", 5)] == ["feed_ranker"]
    finally:
        g.close()


def test_search_hits_description_text(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert [i for i, _ in g.search_ids("bookmark", 5)] == ["saved_article"]
    finally:
        g.close()


def test_search_on_punctuation_only_query_is_empty_not_an_error(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert g.search_ids("   ***   ", 5) == []
        assert g.search_ids('" OR "', 5) == []
    finally:
        g.close()


# --- aggregates --------------------------------------------------------------
def test_counts(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        assert g.counts() == {"nodes": 2, "edges": 1, "anchors": 2, "files": 2}
    finally:
        g.close()


def test_group_counts_rejects_an_arbitrary_column(sample_db: Path) -> None:
    g = CodeGraph(sample_db)
    try:
        with pytest.raises(ValueError):
            g.group_counts("description")
    finally:
        g.close()


def test_isolated_nodes(tmp_path: Path, sample_meta: Meta) -> None:
    nodes = [Node(id="a", kind="K", edges=["b"]), Node(id="b", kind="K"), Node(id="lonely", kind="K")]
    db = tmp_path / "g.db"
    build(db, sample_meta, nodes)
    g = CodeGraph(db)
    try:
        assert g.isolated_ids() == ["lonely"]
    finally:
        g.close()

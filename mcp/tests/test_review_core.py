"""Regression tests for the graph-core findings in the code review (section A, 5-17, 42, 43)."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from codebase_kg import build as build_cli
from codebase_kg import (
    codec,
    core,
    edits,
    links,
    markdown,
    migrate,
    query,
    tools,
    upgrade,
    writer,
)
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.schema import SCHEMA_VERSION
from codebase_kg.store import CodeGraph


def _write_graph(
    dest: Path, nodes: list[Node] | None = None, meta: Meta | None = None, **kwargs: object
) -> Path:
    nodes = nodes if nodes is not None else [
        Node(id="a", kind="Domain", description="Does a thing.", anchors=[Anchor("src/A.kt", "A")])
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer.build(dest, meta or Meta(codebase="x", root="", generated="2026-01-01"), nodes, **kwargs)  # type: ignore[arg-type]
    return dest


def _meta_rows(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()


def _sources(db: Path) -> dict[str, str]:
    g = CodeGraph(db)
    try:
        return g.sources()
    finally:
        g.close()


class _Stdin:
    """A stdin that decodes as cp1252 the way a Windows console pipe does."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)
        self._text = io.TextIOWrapper(io.BytesIO(data), encoding="cp1252", errors="replace")

    def read(self) -> str:
        return self._text.read()

    def isatty(self) -> bool:
        return False


# 5


def test_upgrade_reads_a_graph_given_by_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_graph(tmp_path / "code_graph.db")
    monkeypatch.chdir(tmp_path)
    version, _, nodes, _ = upgrade.read_any_version(Path("code_graph.db"))
    assert version == SCHEMA_VERSION
    assert [n.id for n in nodes] == ["a"]


# 6


def test_upgrade_refuses_a_newer_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _write_graph(tmp_path / "code_graph.db")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    before = db.read_bytes()
    assert upgrade.main([str(db), "--force"]) == 1
    assert db.read_bytes() == before
    assert "newer" in capsys.readouterr().err


# 7


def test_link_resolve_survives_uri_special_characters_in_the_path(tmp_path: Path) -> None:
    knowledge = tmp_path / "a#b%20c" / "knowledge"
    _write_graph(knowledge / "code_graph.db")
    assert links.resolve(knowledge, "code_graph.db#a") is links.Resolution.OK
    assert links.resolve(knowledge, "code_graph.db#missing") is links.Resolution.DANGLING


def test_link_resolve_accepts_a_relative_knowledge_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_graph(tmp_path / "knowledge" / "code_graph.db")
    monkeypatch.chdir(tmp_path)
    assert links.resolve("knowledge", "code_graph.db#a") is links.Resolution.OK


# 8


def test_meta_extra_cannot_overwrite_reserved_keys(tmp_path: Path) -> None:
    meta = Meta(
        codebase="real",
        root="app",
        generated="2026-01-01",
        extra={
            "schema_version": "99",
            "codebase": "forged",
            "root": "elsewhere",
            "generated": "never",
            "owner": "team",
        },
    )
    db = _write_graph(tmp_path / "code_graph.db", meta=meta)
    rows = _meta_rows(db)
    assert rows["schema_version"] == str(SCHEMA_VERSION)
    assert rows["codebase"] == "real"
    assert rows["root"] == "app"
    assert rows["generated"] == "2026-01-01"
    assert rows["owner"] == "team"


# 9


def test_rebaseline_without_a_source_tree_keeps_recorded_baselines(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.kt").write_text("class A", encoding="utf-8")
    db = _write_graph(tmp_path / "code_graph.db", source_root=tmp_path)
    sources = _sources(db)
    assert sources

    report = writer.build(
        db,
        Meta(codebase="x", generated="2026-01-01"),
        [Node(id="a", kind="Domain", description="Does a thing.", anchors=[Anchor("src/A.kt", "A")])],
        sources=sources,
        rebaseline=True,
    )
    assert report.hashed == len(sources)
    assert _sources(db) == sources


# 10


def _doc(description: str) -> bytes:
    doc = {
        "codebase": "x",
        "generated": "2026-01-01",
        "nodes": [{"id": "a", "kind": "Domain", "description": description}],
    }
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def test_build_reads_stdin_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(_doc("Rendé café — prêt.")))
    out = tmp_path / "code_graph.db"
    assert build_cli.main(["-", "-o", str(out), "--no-hash"]) == 0
    g = CodeGraph(out)
    try:
        assert g.node("a").description == "Rendé café — prêt."  # type: ignore[union-attr]
    finally:
        g.close()


def test_build_reads_a_utf16_file_with_a_bom(tmp_path: Path) -> None:
    src = tmp_path / "graph.json"
    src.write_bytes(b"\xff\xfe" + _doc("Plain description.").decode("utf-8").encode("utf-16-le"))
    out = tmp_path / "code_graph.db"
    assert build_cli.main([str(src), "-o", str(out), "--no-hash"]) == 0


def test_build_reports_undecodable_input_instead_of_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "graph.json"
    src.write_bytes(b'{"nodes": "\x80\x81"}')
    assert build_cli.main([str(src), "-o", str(tmp_path / "x.db")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_query_reads_stdin_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _write_graph(tmp_path / "code_graph.db")
    seen: list[str] = []

    def fake_search(_g: object, text: str, _kind: object = None) -> dict[str, object]:
        seen.append(text)
        return {}

    monkeypatch.setattr(query.tools, "kg_search", fake_search)
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps({"query": "naïve"}, ensure_ascii=False).encode("utf-8")))
    assert query.main(["--graph", str(db), "kg_search", "-"]) == 0
    assert seen == ["naïve"]
    capsys.readouterr()


# 11


def test_markdown_root_dot_is_normalized(tmp_path: Path) -> None:
    meta = markdown.parse_meta("codebase: demo\nroot: .\n")
    assert meta.root == ""


def test_migrate_writes_no_dot_root(tmp_path: Path) -> None:
    md = tmp_path / "KNOWLEDGE_GRAPH.md"
    md.write_text(
        "codebase: demo\nroot: .\n\n## Nodes\n\n| id | kind | description |\n|---|---|---|\n"
        "| a | Domain | Does a thing. |\n",
        encoding="utf-8",
    )
    out = tmp_path / "code_graph.db"
    migrate.convert(md, out)
    assert _meta_rows(out).get("root", "") == ""


# 12


def _legacy_markdown(tmp_path: Path) -> Path:
    md = tmp_path / "KNOWLEDGE_GRAPH.md"
    md.write_text(
        "codebase: demo\n\n## Nodes\n\n| id | kind | description |\n|---|---|---|\n"
        "| legacy | Domain | Old node. |\n",
        encoding="utf-8",
    )
    return md


def _node_ids(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM node ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("explicit_out", [False, True])
def test_migrate_refuses_to_replace_an_existing_file_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], explicit_out: bool
) -> None:
    md = _legacy_markdown(tmp_path)
    db = _write_graph(tmp_path / "code_graph.db")
    before = db.read_bytes()
    argv = [str(md), "-o", str(db)] if explicit_out else [str(md)]
    assert migrate.main(argv) == 1
    assert db.read_bytes() == before
    err = capsys.readouterr().err
    assert str(db) in err
    assert "--force" in err


@pytest.mark.parametrize("explicit_out", [False, True])
def test_migrate_force_overwrites_an_existing_file(tmp_path: Path, explicit_out: bool) -> None:
    md = _legacy_markdown(tmp_path)
    db = _write_graph(tmp_path / "code_graph.db")
    argv = [str(md), "-o", str(db), "--force"] if explicit_out else [str(md), "--force"]
    assert migrate.main(argv) == 0
    assert _node_ids(db) == ["legacy"]


def test_migrate_writes_a_new_file_without_force(tmp_path: Path) -> None:
    md = _legacy_markdown(tmp_path)
    out = tmp_path / "out" / "g.db"
    out.parent.mkdir()
    assert migrate.main([str(md), "-o", str(out)]) == 0
    assert _node_ids(out) == ["legacy"]


# 13


@pytest.mark.parametrize(
    ("symbol", "source"),
    [
        ("@Model", "@Model\nclass Thing"),
        ("$state", "let $state = 1"),
        ("operator==", "fun operator==(a: A)"),
        ("Plain", "class Plain"),
        ("Outer.inner", "class Outer { fun inner() }"),
    ],
)
def test_symbols_with_non_word_edges_are_found_in_source(symbol: str, source: str) -> None:
    assert tools._symbol_in_source(symbol, source)


def test_a_word_symbol_still_needs_word_boundaries() -> None:
    assert not tools._symbol_in_source("Plain", "class PlainOld")
    assert not tools._symbol_in_source("Plain", "class OldPlain")


def test_upsert_accepts_an_anchor_whose_symbol_starts_with_a_sigil(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "Thing.kt").write_text("@Model\nclass Thing\n", encoding="utf-8")
    db = tmp_path / "knowledge" / "code_graph.db"
    _write_graph(
        db,
        [Node(id="t", kind="Entity", description="A thing.", anchors=[Anchor("Thing.kt", "Thing")])],
        Meta(codebase="x", root="src", generated="2026-01-01"),
        source_root=src,
    )
    result = edits.upsert_node(db, [{"id": "t", "anchors": ["Thing.kt#@Model"]}])
    assert result["written"] is True


# 15


def test_an_edit_is_refused_when_the_graph_changed_underneath_it(tmp_path: Path) -> None:
    db = _write_graph(tmp_path / "code_graph.db")

    def mutate(conn: sqlite3.Connection) -> list[edits.Change]:
        conn.execute("UPDATE node SET kind = 'Edited' WHERE id = 'a'")
        _write_graph(
            db,
            [Node(id="a", kind="Domain", description="Written by another process.")],
        )
        return [edits.Change("node", "a", "updated")]

    with pytest.raises(edits.EditError, match="changed while"):
        edits.apply(db, mutate)
    g = CodeGraph(db)
    try:
        assert g.node("a").description == "Written by another process."  # type: ignore[union-attr]
    finally:
        g.close()


# 16, 17


def _search_graph(tmp_path: Path) -> CodeGraph:
    db = _write_graph(
        tmp_path / "code_graph.db",
        [
            Node(id="cv", kind="Domain", description="Builds the résumé page."),
            Node(id="jp", kind="Domain", description="日本語 support."),
            Node(id="pct", kind="100%_sure", description="Plain."),
            Node(id="other", kind="Entity", description="Plain."),
        ],
    )
    return CodeGraph(db)


def test_search_matches_non_ascii_words(tmp_path: Path) -> None:
    g = _search_graph(tmp_path)
    try:
        assert [i for i, _ in g.search_ids("résumé", 5)] == ["cv"]
        assert [i for i, _ in g.search_ids("日本語", 5)] == ["jp"]
    finally:
        g.close()


def test_like_wildcards_in_a_kind_are_literal(tmp_path: Path) -> None:
    g = _search_graph(tmp_path)
    try:
        assert [n.id for n in g.by_kind("%")] == ["pct"]
        assert [n.id for n in g.by_kind("_")] == ["pct"]
        assert [n.id for n in g.by_kind("domain")] == ["cv", "jp"]
    finally:
        g.close()


def test_like_wildcards_in_a_did_you_mean_lookup_are_literal(tmp_path: Path) -> None:
    g = _search_graph(tmp_path)
    try:
        assert g.similar_ids("%") == []
    finally:
        g.close()


# 42


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src\\A.kt", "src/A.kt"),
        ("./src/A.kt", "src/A.kt"),
        ("src//A.kt", "src/A.kt"),
        ("src/./A.kt", "src/A.kt"),
        ("src/gen/../A.kt", "src/A.kt"),
        ("../shared/A.kt", "../shared/A.kt"),
    ],
)
def test_anchor_paths_are_normalized_as_posix(raw: str, expected: str) -> None:
    assert Anchor(raw).path == expected
    assert Anchor.parse(f"{raw}#Sym").path == expected


def test_reference_paths_normalize_like_anchor_paths() -> None:
    from codebase_kg.models import Reference

    assert Reference(url="https://x.test", path=".\\src\\A.kt").path == "src/A.kt"


def test_counterpart_paths_are_stored_with_forward_slashes(tmp_path: Path) -> None:
    node = Node(
        id="a", kind="Domain", description="Does a thing.",
        parity="matched", counterpart="..\\ios\\code_graph.db#a",
    )
    db = _write_graph(
        tmp_path / "code_graph.db", [node],
        Meta(codebase="x", generated="2026-01-01", counterpart="..\\ios\\code_graph.db"),
    )
    g = CodeGraph(db)
    try:
        assert g.node("a").counterpart == "../ios/code_graph.db#a"  # type: ignore[union-attr]
        assert g.meta.counterpart == "../ios/code_graph.db"
    finally:
        g.close()


def test_upsert_normalizes_a_counterpart_path(tmp_path: Path) -> None:
    db = _write_graph(tmp_path / "knowledge" / "code_graph.db")
    peer = _write_graph(tmp_path / "ios" / "code_graph.db")
    assert peer.is_file()
    edits.upsert_node(
        db, [{"id": "a", "parity": "matched", "counterpart": "..\\ios\\code_graph.db#a"}]
    )
    g = CodeGraph(db)
    try:
        assert g.node("a").counterpart == "../ios/code_graph.db#a"  # type: ignore[union-attr]
    finally:
        g.close()


def test_a_null_anchor_path_is_refused_not_stored_as_none(tmp_path: Path) -> None:
    db = _write_graph(tmp_path / "code_graph.db")
    with pytest.raises(edits.EditError, match="no path"):
        edits.upsert_node(db, [{"id": "a", "anchors": [{"path": None, "symbol": "X"}]}])


def test_empty_counterpart_is_treated_as_absent_by_the_writer(tmp_path: Path) -> None:
    node = Node(id="a", kind="Domain", description="Does a thing.", parity="ios-only", counterpart="")
    db = _write_graph(tmp_path / "code_graph.db", [node])
    g = CodeGraph(db)
    try:
        assert g.node("a").counterpart is None  # type: ignore[union-attr]
    finally:
        g.close()


def test_upsert_with_an_empty_counterpart_clears_it(tmp_path: Path) -> None:
    db = _write_graph(tmp_path / "code_graph.db")
    edits.upsert_node(db, [{"id": "a", "parity": "ios-only", "counterpart": ""}])
    g = CodeGraph(db)
    try:
        assert g.node("a").counterpart is None  # type: ignore[union-attr]
    finally:
        g.close()


def test_parity_gap_status_filter_ignores_case(tmp_path: Path) -> None:
    node = Node(id="a", kind="Domain", description="Does a thing.", parity="Android-only")
    db = _write_graph(tmp_path / "code_graph.db", [node])
    g = CodeGraph(db)
    try:
        for status in ("android-only", "Android-only", "only"):
            assert [x["id"] for x in tools.kg_parity_gaps(g, status)["gaps"]] == ["a"]
    finally:
        g.close()


def test_peer_node_table_name_is_quoted(tmp_path: Path) -> None:
    peer = tmp_path / "peer.db"
    conn = sqlite3.connect(peer)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('node_table', 'my nodes')")
    conn.execute('CREATE TABLE "my nodes" (id TEXT)')
    conn.execute("INSERT INTO \"my nodes\" VALUES ('x')")
    conn.commit()
    conn.close()
    assert links.resolve(tmp_path, "peer.db#x") is links.Resolution.OK
    assert links.resolve(tmp_path, "peer.db#y") is links.Resolution.DANGLING


# --- self-edges ---------------------------------------------------------------
def _self_edge_graph(dest: Path) -> Path:
    return _write_graph(
        dest,
        [
            Node(id="a", kind="K", description="Calls itself.", edges=["a", "b"]),
            Node(id="b", kind="K", description="Plain."),
            Node(id="solo", kind="K", description="Only points at itself.", edges=["solo"]),
        ],
    )


def test_a_self_edge_survives_a_build_and_reads_back(tmp_path: Path) -> None:
    db = _self_edge_graph(tmp_path / "code_graph.db")
    g = CodeGraph(db)
    try:
        assert g.node("a").edges == ["a", "b"]  # type: ignore[union-attr]
        assert g.counts()["edges"] == 3
        assert tools.kg_node(g, "a")["inbound_edges"] == ["a"]
    finally:
        g.close()


def test_a_node_whose_only_edge_is_a_self_edge_is_still_isolated(tmp_path: Path) -> None:
    db = _self_edge_graph(tmp_path / "code_graph.db")
    g = CodeGraph(db)
    try:
        assert g.isolated_ids() == ["solo"]
        assert g.isolated_count() == 1
    finally:
        g.close()


def test_a_neighborhood_walk_terminates_on_a_self_edge(tmp_path: Path) -> None:
    db = _self_edge_graph(tmp_path / "code_graph.db")
    g = CodeGraph(db)
    try:
        assert g.neighborhood_ids("a", 3) == {"a": 0, "b": 1}
        found = tools.kg_neighborhood(g, "a", depth=3)
        assert [n["id"] for n in found["neighbors"]] == ["b"]
    finally:
        g.close()


def test_a_self_edge_round_trips_through_export_and_build(tmp_path: Path) -> None:
    db = _self_edge_graph(tmp_path / "code_graph.db")
    g = CodeGraph(db)
    try:
        doc = codec.to_dict(g.meta, g.all_nodes(), g.sources())
    finally:
        g.close()
    meta, nodes, sources = codec.from_dict(json.loads(json.dumps(doc)))
    rebuilt = tmp_path / "again.db"
    report = writer.build(rebuilt, meta, nodes, sources=sources)
    assert report.edges == 3
    assert report.dropped_edges == []


def test_deleting_a_node_does_not_count_its_own_self_edge_as_inbound(tmp_path: Path) -> None:
    db = _self_edge_graph(tmp_path / "code_graph.db")
    preview = edits.delete_node(db, ["solo"], dry_run=True)
    assert preview["would_delete"]["inbound_edges"] == []
    edits.delete_node(db, ["solo"], dry_run=False)
    g = CodeGraph(db)
    try:
        assert g.node("solo") is None
        assert g.counts()["edges"] == 2
    finally:
        g.close()


def test_another_nodes_edge_into_a_node_with_a_self_edge_still_blocks_its_delete(
    tmp_path: Path,
) -> None:
    db = _write_graph(
        tmp_path / "code_graph.db",
        [
            Node(id="a", kind="K", description="Calls itself.", edges=["a"]),
            Node(id="b", kind="K", description="Calls a.", edges=["a"]),
        ],
    )
    with pytest.raises(edits.EditError, match=r"b -> a"):
        edits.delete_node(db, ["a"], dry_run=False)
    edits.delete_node(db, ["a"], dry_run=False, cascade_inbound=True)
    g = CodeGraph(db)
    try:
        assert g.node("a") is None
        assert g.node("b").edges == []  # type: ignore[union-attr]
    finally:
        g.close()


def _as_graph_before_self_edges(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT src, dst FROM edge").fetchall()
        conn.execute("DROP TABLE edge")
        conn.execute(
            "CREATE TABLE edge (src TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,"
            " dst TEXT NOT NULL REFERENCES node(id) ON DELETE RESTRICT,"
            " PRIMARY KEY (src, dst), CHECK (src <> dst)) WITHOUT ROWID"
        )
        conn.execute("CREATE INDEX edge_dst ON edge(dst)")
        conn.executemany("INSERT INTO edge (src, dst) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def test_a_self_edge_on_a_graph_that_predates_them_names_the_upgrade(tmp_path: Path) -> None:
    db = _write_graph(
        tmp_path / "code_graph.db",
        [Node(id="a", kind="K", description="One."), Node(id="b", kind="K", description="Two.")],
    )
    _as_graph_before_self_edges(db)
    before = db.read_bytes()
    with pytest.raises(edits.EditError, match=r"predates self-edges.*upgrade.*--force"):
        edits.upsert_node(db, [{"id": "a", "edges": ["a"]}])
    assert db.read_bytes() == before

    assert upgrade.main([str(db), "--force"]) == 0
    edits.upsert_node(db, [{"id": "a", "edges": ["a"]}])
    g = CodeGraph(db)
    try:
        assert g.node("a").edges == ["a"]  # type: ignore[union-attr]
    finally:
        g.close()


# --- unknown keys -------------------------------------------------------------
def _two_nodes(tmp_path: Path) -> Path:
    return _write_graph(
        tmp_path / "code_graph.db",
        [Node(id="a", kind="K", description="One."), Node(id="b", kind="K", description="Two.")],
    )


def test_a_misspelled_node_key_is_refused_with_a_hint_and_writes_nothing(tmp_path: Path) -> None:
    db = _two_nodes(tmp_path)
    before = db.read_bytes()
    with pytest.raises(edits.EditError) as caught:
        edits.upsert_node(
            db, [{"id": "b", "description": "Changed."}, {"id": "a", "anchorz": ["src/A.kt#A"]}]
        )
    message = str(caught.value)
    assert "node 'a'" in message
    assert "unknown key 'anchorz'" in message
    assert "did you mean 'anchors'?" in message
    assert "Accepted keys: id, kind, description" in message
    assert db.read_bytes() == before


def test_an_unknown_key_with_no_close_match_lists_the_accepted_keys(tmp_path: Path) -> None:
    db = _two_nodes(tmp_path)
    with pytest.raises(edits.EditError) as caught:
        edits.upsert_node(db, [{"id": "a", "zzzzzz": 1}])
    assert "unknown key 'zzzzzz'" in str(caught.value)
    assert "did you mean" not in str(caught.value)
    assert "Accepted keys:" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "item", "hint"),
    [
        ("anchors", {"pth": "src/A.kt", "symbol": "A"}, "did you mean 'path'?"),
        ("external_links", {"targt": "peer.db#x"}, "did you mean 'target'?"),
        ("references", {"urll": "https://example.com"}, "did you mean 'url'?"),
    ],
)
def test_an_unknown_key_inside_a_nested_item_is_refused(
    tmp_path: Path, field: str, item: dict[str, str], hint: str
) -> None:
    db = _two_nodes(tmp_path)
    before = db.read_bytes()
    with pytest.raises(edits.EditError, match=hint):
        edits.upsert_node(db, [{"id": "a", field: [item]}])
    assert db.read_bytes() == before


def test_known_keys_in_nested_items_are_still_accepted(tmp_path: Path) -> None:
    db = _two_nodes(tmp_path)
    edits.upsert_node(
        db,
        [
            {
                "id": "a",
                "anchors": [{"path": "src/A.kt", "symbol": "A"}],
                "references": [{"url": "https://example.com", "path": "src/A.kt", "symbol": "A"}],
            }
        ],
    )
    g = CodeGraph(db)
    try:
        node = g.node("a")
        assert node is not None and [str(a) for a in node.anchors] == ["src/A.kt#A"]
    finally:
        g.close()


def test_the_published_schema_forbids_unknown_keys_in_node_items() -> None:
    schema = core.TOOLS["kg_upsert_node"]["inputSchema"]["properties"]["nodes"]["items"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["external_links"]["items"]["additionalProperties"] is False
    assert schema["properties"]["references"]["items"]["additionalProperties"] is False

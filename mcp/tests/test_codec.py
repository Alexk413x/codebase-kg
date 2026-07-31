"""The JSON authoring format: round-trip fidelity and useful rejection messages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_kg import build as build_cli
from codebase_kg import codec, export
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

FIX = Path(__file__).resolve().parent / "fixtures"


# --- round trip --------------------------------------------------------------
def test_round_trip_preserves_every_field() -> None:
    meta = Meta(
        codebase="android", root="src", generated="2026-07-30",
        language="kotlin", counterpart="../ios/code_graph.db",
    )
    nodes = [
        Node(
            id="a", kind="K", description="Does a thing.", section="DOMAIN",
            anchors=[Anchor("x.kt", "X"), Anchor("manifest.xml")], edges=["b"],
            parity="divergent", counterpart="../ios/code_graph.db#a",
            divergence="different shape",
        ),
        Node(id="b", kind="K", description="Other."),
    ]
    meta2, nodes2, _ = codec.from_dict(codec.to_dict(meta, nodes))
    assert meta2 == meta
    assert nodes2 == nodes


def test_whole_file_anchor_survives_the_round_trip() -> None:
    nodes = [Node(id="a", kind="K", anchors=[Anchor("AndroidManifest.xml")])]
    _, out, _ = codec.from_dict(codec.to_dict(Meta(), nodes))
    assert out[0].anchors == [Anchor("AndroidManifest.xml", None)]


def test_export_then_build_is_byte_identical(
    built_fixtures: Path, tmp_path: Path
) -> None:
    # The refresh loop is export -> edit -> build; an unedited pass through it
    # has to be a no-op, or every refresh would churn the committed file.
    src = built_fixtures / "android" / "code_graph.db"
    g = CodeGraph(src)
    try:
        doc = codec.to_dict(g.meta, g.all_nodes(), g.sources())
    finally:
        g.close()
    meta, nodes, sources = codec.from_dict(doc)
    rebuilt = tmp_path / "code_graph.db"
    build(rebuilt, meta, nodes, sources=sources)
    assert rebuilt.read_bytes() == src.read_bytes()


# --- tolerant decoding -------------------------------------------------------
def test_missing_optional_fields_default() -> None:
    _, nodes, _ = codec.from_dict({"nodes": [{"id": "a", "kind": "K"}]})
    n = nodes[0]
    assert n.description == "" and n.anchors == [] and n.edges == []
    assert n.parity is None and n.section == ""


def test_a_bare_string_is_accepted_where_a_list_belongs() -> None:
    _, nodes, _ = codec.from_dict({"nodes": [{"id": "a", "kind": "K", "edges": "b"}]})
    assert nodes[0].edges == ["b"]


def test_unknown_top_level_config_is_carried_not_dropped() -> None:
    meta, _, _ = codec.from_dict({"team": "mobile", "nodes": []})
    assert meta.extra["team"] == "mobile"


def test_whitespace_is_trimmed() -> None:
    _, nodes, _ = codec.from_dict({"nodes": [{"id": "  a  ", "kind": " K "}]})
    assert nodes[0].id == "a" and nodes[0].kind == "K"


# --- rejection messages ------------------------------------------------------
def test_non_object_top_level_rejected() -> None:
    with pytest.raises(codec.DecodeError, match="top level"):
        codec.from_dict([])


def test_missing_nodes_list_rejected() -> None:
    with pytest.raises(codec.DecodeError, match="`nodes` must be a list"):
        codec.from_dict({"codebase": "x"})


def test_node_without_an_id_names_its_position() -> None:
    with pytest.raises(codec.DecodeError, match="node 0: `id` is required"):
        codec.from_dict({"nodes": [{"kind": "K"}]})


def test_bad_field_type_names_the_node() -> None:
    with pytest.raises(codec.DecodeError, match="node 'a': `anchors` must be a list"):
        codec.from_dict({"nodes": [{"id": "a", "kind": "K", "anchors": [1, 2]}]})


# --- build CLI ---------------------------------------------------------------
def _doc(**over: object) -> dict[str, object]:
    doc = {
        "codebase": "x", "root": "src", "generated": "2026-07-30",
        "nodes": [
            {"id": "a", "kind": "K", "description": "One.", "edges": ["b"],
             "anchors": ["x.kt#X"]},
            {"id": "b", "kind": "K", "description": "Two."},
        ],
    }
    doc.update(over)
    return doc


def test_build_cli_writes_the_store(tmp_path: Path) -> None:
    src = tmp_path / "graph.json"
    src.write_text(json.dumps(_doc()), encoding="utf-8")
    out = tmp_path / "code_graph.db"
    assert build_cli.main([str(src), "-o", str(out)]) == 0
    g = CodeGraph(out)
    try:
        assert g.counts()["nodes"] == 2
    finally:
        g.close()


def test_build_cli_reports_a_bad_description_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    doc = _doc(nodes=[{"id": "a", "kind": "K", "description": "Wires it (ACME-1)."}])
    src = tmp_path / "graph.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "code_graph.db"
    assert build_cli.main([str(src), "-o", str(out)]) == 1
    err = capsys.readouterr().err
    assert "node 'a'" in err and "ticket refs" in err
    assert not out.exists()


def test_build_cli_rejects_a_dangling_edge_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    doc = _doc(nodes=[{"id": "a", "kind": "K", "edges": ["ghost"]}])
    src = tmp_path / "graph.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    assert build_cli.main([str(src), "-o", str(tmp_path / "g.db")]) == 1
    assert "dangling edge" in capsys.readouterr().err


def test_build_cli_can_drop_dangling_edges_on_request(tmp_path: Path) -> None:
    doc = _doc(nodes=[{"id": "a", "kind": "K", "edges": ["ghost"]}])
    src = tmp_path / "graph.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "g.db"
    assert build_cli.main([str(src), "-o", str(out), "--allow-dangling"]) == 0
    assert out.is_file()


def test_build_cli_reports_unreadable_input(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert build_cli.main([str(tmp_path / "nope.json")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_build_cli_reports_malformed_json(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    src = tmp_path / "graph.json"
    src.write_text("{not json", encoding="utf-8")
    assert build_cli.main([str(src)]) == 1
    assert "cannot read" in capsys.readouterr().err


# --- export CLI --------------------------------------------------------------
def test_export_cli_writes_json(built_fixtures: Path, tmp_path: Path) -> None:
    out = tmp_path / "graph.json"
    assert export.main([str(built_fixtures / "android" / "code_graph.db"), "-o", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["codebase"] == "android"
    assert {n["id"] for n in doc["nodes"]} == {
        "saved_article", "bookmarks_screen", "feed_ranker", "night_digest",
    }


def test_export_cli_subset(built_fixtures: Path, capsys: pytest.CaptureFixture) -> None:
    assert export.main(
        [str(built_fixtures / "android" / "code_graph.db"), "--ids", "feed_ranker"]
    ) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [n["id"] for n in doc["nodes"]] == ["feed_ranker"]


def test_export_cli_warns_about_unknown_ids(
    built_fixtures: Path, capsys: pytest.CaptureFixture
) -> None:
    export.main([str(built_fixtures / "android" / "code_graph.db"), "--ids", "nope"])
    assert "no such node" in capsys.readouterr().err


def test_export_cli_reports_a_missing_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert export.main([str(tmp_path / "nope.db")]) == 1
    assert "no code graph" in capsys.readouterr().err

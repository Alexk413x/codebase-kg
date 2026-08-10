"""Cross-graph links — the portable mechanism, and this graph's use of it.

`links.py` is a **copy**, byte-identical to cartographer's. The specification is
`docs/GRAPH-LINKS.md` in that repo. These tests are the local half of keeping the
copies honest: the DDL below is quoted from the spec, so an edit to this repo's
copy fails here rather than in whatever graph reads the file next.

What this table is *not* is a change to `counterpart`. Those CHECKs encode parity
between two codebases (SCHEMA.md §9) and are the reason a half-filled parity
triple is a failed write. This sits beside them, additively — the tests at the
bottom prove nothing about parity moved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codebase_kg import codec, links, tools, upgrade
from codebase_kg.links import ExternalLink, LinkError, Resolution
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.schema import DDL
from codebase_kg.store import CodeGraph
from codebase_kg.writer import BuildError, build

#: Quoted from cartographer's docs/GRAPH-LINKS.md §1. If this stops matching
#: `links.EXTERNAL_LINK_DDL`, one of the two was edited on its own and the graphs
#: have stopped speaking the same mechanism.
PUBLISHED_DDL = """\
CREATE TABLE external_link (
    node_id TEXT NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    target  TEXT NOT NULL,   -- "<db-file>#<node-id>"
    kind    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, target),
    CHECK (target <> ''),
    CHECK (instr(target, '#') > 1)
) WITHOUT ROWID;

CREATE INDEX external_link_target ON external_link(target);
"""


def _screen_graph(
    path: Path, node_ids: list[str], *, table: str = "screen", declare: bool = True
) -> Path:
    """A stand-in for cartographer's committed cartographer_graph.db.

    The entity table is `screen`, not `node`. This fixture used to build `node`
    — the one table cartographer v3 does not have — which is precisely why
    `resolve` could hardcode that name and every test here still pass, while a
    real link into a real screen graph came back `peer-unreadable`.

    The contract is an entity table with a TEXT `id`, named in `meta.node_table`.
    `table`/`declare` vary it so the probe path (artifacts written before that key
    existed) stays covered too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    con.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in node_ids])
    if declare:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO meta VALUES (?, ?)", (links.NODE_TABLE_KEY, table))
    con.commit()
    con.close()
    return path


#: What the three real graphs call their entity, and whether the artifact
#: declares it. `screen` and `action` undeclared are the shapes committed before
#: `meta.node_table` existed; they must keep resolving or every link into an
#: already-committed artifact breaks.
PEER_SHAPES = [("node", False), ("screen", False), ("screen", True), ("action", True)]


# --- the copies must not drift ------------------------------------------------
def test_this_copy_matches_the_published_ddl() -> None:
    assert links.EXTERNAL_LINK_DDL == PUBLISHED_DDL


def test_this_copy_declares_the_spec_version_it_implements() -> None:
    assert links.LINKS_SPEC_VERSION == 1


def test_the_table_is_part_of_this_graphs_schema() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(DDL)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "external_link" in names
    # The reverse question -- "which code presents this screen?" -- reads
    # `target`, which is not the leading key column. Without the index it scans.
    assert "external_link_target" in names
    con.close()


# --- the table's own constraints ----------------------------------------------
@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(DDL)
    c.execute("INSERT INTO node (id, kind, description) VALUES ('a', 'K', 'One.')")
    yield c
    c.close()


def test_a_link_off_a_node_that_does_not_exist_is_impossible(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO external_link (node_id, target) VALUES ('ghost', 'c.db#x')")


def test_a_target_with_no_node_fragment_is_impossible(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO external_link (node_id, target) VALUES ('a', 'c.db')")


def test_a_target_with_no_database_part_is_impossible(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO external_link (node_id, target) VALUES ('a', '#x')")


def test_deleting_a_node_takes_its_links_with_it(conn) -> None:
    conn.execute("INSERT INTO external_link (node_id, target) VALUES ('a', 'c.db#x')")
    conn.execute("DELETE FROM node WHERE id = 'a'")
    assert conn.execute("SELECT count(*) FROM external_link").fetchone()[0] == 0


def test_one_node_cannot_give_one_target_two_kinds(conn) -> None:
    conn.execute("INSERT INTO external_link (node_id, target, kind) VALUES ('a','c.db#x','a')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO external_link (node_id, target, kind) VALUES ('a','c.db#x','b')"
        )


# --- the URI convention -------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("cartographer_graph.db#rpn-main", "cartographer_graph.db#rpn-main"),
        (r"sub\dir\g.db#node", "sub/dir/g.db#node"),
        ("knowledge/cartographer_graph.db#x", "cartographer_graph.db#x"),
    ],
)
def test_a_target_is_stored_in_one_canonical_form(raw, expected) -> None:
    """A link written on Windows must be the bytes a Mac reads, or the reverse
    lookup -- an indexed equality probe -- silently returns nothing."""
    assert ExternalLink(raw).target == expected


@pytest.mark.parametrize("bad", ["g.db", "#x", "g.db#", ""])
def test_a_target_missing_either_half_is_refused(bad) -> None:
    with pytest.raises(LinkError):
        links.split_target(bad)


def test_the_writer_names_the_node_when_a_target_is_unusable(tmp_path: Path) -> None:
    """An empty fragment is the one malformed shape the CHECK cannot express, so
    the writer catches it -- naming the node, not raising a bare IntegrityError."""
    with pytest.raises(BuildError, match="rpn_screen"):
        build(
            tmp_path / "g.db",
            Meta(codebase="x", root="src"),
            [Node(id="rpn_screen", kind="Composable", links=[ExternalLink("g.db#")])],
        )


def test_the_writer_names_the_node_when_one_target_carries_two_kinds(tmp_path: Path) -> None:
    """Reported before the write, not raised from inside the insert loop after
    validation has already called the input clean."""
    with pytest.raises(BuildError, match="different kinds"):
        build(
            tmp_path / "g.db",
            Meta(codebase="x", root="src"),
            [
                Node(
                    id="rpn_screen",
                    kind="Composable",
                    links=[
                        ExternalLink("c.db#x", "implements"),
                        ExternalLink("c.db#x", "tests"),
                    ],
                )
            ],
        )


# --- resolution grading -------------------------------------------------------
@pytest.mark.parametrize("table, declare", PEER_SHAPES)
def test_a_present_peer_holding_the_node_resolves(
    tmp_path: Path, table: str, declare: bool
) -> None:
    """Whatever the peer calls its entity. `resolve` used to ask every graph for
    `node`, which is this graph's name and nobody else's."""
    _screen_graph(
        tmp_path / "cartographer_graph.db", ["rpn-main"], table=table, declare=declare
    )
    assert links.resolve(tmp_path, "cartographer_graph.db#rpn-main") is Resolution.OK


def test_an_absent_peer_is_a_warning_because_cartographer_is_optional(tmp_path: Path) -> None:
    outcome = links.resolve(tmp_path, "cartographer_graph.db#rpn-main")
    assert outcome is Resolution.PEER_ABSENT and not outcome.is_error


@pytest.mark.parametrize("table, declare", PEER_SHAPES)
def test_a_present_peer_missing_the_node_is_an_error(
    tmp_path: Path, table: str, declare: bool
) -> None:
    """The half that actually cost something: a peer whose entity table we could
    not name graded every broken pointer into it as a warning."""
    _screen_graph(
        tmp_path / "cartographer_graph.db", ["rpn-main"], table=table, declare=declare
    )
    outcome = links.resolve(tmp_path, "cartographer_graph.db#typo")
    assert outcome is Resolution.DANGLING and outcome.is_error


def test_a_declared_node_table_beats_the_probe(tmp_path: Path) -> None:
    """A graph carrying both a legacy-named table and a declaration is read the
    way it declares itself -- otherwise the probe order, not the graph, decides
    which of its tables is the entity."""
    path = _screen_graph(tmp_path / "cartographer_graph.db", ["real"])
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE node (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO node VALUES ('decoy')")
    con.commit()
    con.close()
    assert links.resolve(tmp_path, "cartographer_graph.db#real") is Resolution.OK
    assert links.resolve(tmp_path, "cartographer_graph.db#decoy") is Resolution.DANGLING


def test_a_file_that_is_not_a_graph_is_unknown_not_broken(tmp_path: Path) -> None:
    (tmp_path / "cartographer_graph.db").write_bytes(b"not a database")
    outcome = links.resolve(tmp_path, "cartographer_graph.db#x")
    assert outcome is Resolution.PEER_UNREADABLE and not outcome.is_error


def test_a_database_with_no_entity_table_is_unknown_not_broken(tmp_path: Path) -> None:
    """Openable, but nothing in it answers "is this id yours?"."""
    con = sqlite3.connect(tmp_path / "cartographer_graph.db")
    con.execute("CREATE TABLE unrelated (x TEXT)")
    con.commit()
    con.close()
    outcome = links.resolve(tmp_path, "cartographer_graph.db#x")
    assert outcome is Resolution.PEER_UNREADABLE and not outcome.is_error


def test_this_graph_names_its_own_entity_table_so_a_peer_can_resolve_into_it(
    tmp_path: Path,
) -> None:
    """The inbound half: cartographer follows a link into `code_graph.db` and
    must get ok-or-dangling. Run against a real built graph, because a fixture
    that asserts its own shape is what hid the outbound bug."""
    db = _linked_graph(tmp_path)
    con = sqlite3.connect(db)
    assert links.peer_node_table(con) == "node"
    assert con.execute(
        "SELECT value FROM meta WHERE key = ?", (links.NODE_TABLE_KEY,)
    ).fetchone() == ("node",), "declared, not merely probeable"
    con.close()
    assert links.resolve(db.parent, "code_graph.db#rpn_screen") is Resolution.OK
    assert links.resolve(db.parent, "code_graph.db#gone").is_error


# --- round trip and both directions -------------------------------------------
def _linked_graph(tmp_path: Path) -> Path:
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="android", root="app/src"),
        [
            Node(
                id="rpn_screen",
                kind="Composable",
                description="The RPN calculator's main screen.",
                anchors=[Anchor("main/RpnScreen.kt", "RpnScreen")],
                links=[ExternalLink("cartographer_graph.db#rpn-main", "presented-by")],
            ),
            Node(
                id="rpn_view_model",
                kind="ViewModel",
                description="Holds the RPN stack.",
                links=[ExternalLink("cartographer_graph.db#rpn-main", "presented-by")],
            ),
        ],
    )
    return db


def test_a_link_survives_the_export_build_round_trip(tmp_path: Path) -> None:
    """A refresh is export -> edit -> build; a field the codec drops is a field
    that silently disappears the first time anyone edits an unrelated node."""
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        doc = codec.to_dict(g.meta, g.all_nodes(), g.sources())
    finally:
        g.close()

    _, nodes, _ = codec.from_dict(doc)
    assert nodes[0].links == [ExternalLink("cartographer_graph.db#rpn-main", "presented-by")]

    rebuilt = tmp_path / "knowledge" / "again.db"
    build(rebuilt, *codec.from_dict(doc)[:2])
    g2 = CodeGraph(rebuilt)
    try:
        assert g2.node("rpn_screen").links == nodes[0].links
    finally:
        g2.close()


def test_the_forward_direction_says_what_this_code_presents(tmp_path: Path) -> None:
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        assert [link.as_dict() for link in g.node("rpn_screen").links] == [
            {"target": "cartographer_graph.db#rpn-main", "kind": "presented-by"}
        ]
        assert tools.kg_node(g, "rpn_screen")["external_links"] == [
            {"target": "cartographer_graph.db#rpn-main", "kind": "presented-by"}
        ]
    finally:
        g.close()


def test_the_reverse_direction_says_which_code_presents_a_screen(tmp_path: Path) -> None:
    """One stored row answers both questions -- that is what the index is for."""
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        assert g.nodes_linking_to("cartographer_graph.db#rpn-main") == [
            "rpn_screen",
            "rpn_view_model",
        ]
        assert g.nodes_linking_to("rpn-main") == ["rpn_screen", "rpn_view_model"], (
            "a bare peer node id works, since that is what the peer's tools return"
        )
        assert tools.kg_find_by_link(g, "rpn-main")["count"] == 2
        assert tools.kg_find_by_link(g, "cartographer_graph.db#nothing")["note"]
    finally:
        g.close()


# --- validation ---------------------------------------------------------------
def test_a_dangling_link_fails_validation(tmp_path: Path) -> None:
    _screen_graph(tmp_path / "knowledge" / "cartographer_graph.db", ["something-else"])
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        report = tools.kg_validate(g)
    finally:
        g.close()
    assert not report["ok"]
    assert {i["severity"] for i in report["external_link_issues"]} == {"error"}


def test_an_absent_peer_graph_does_not_fail_validation(tmp_path: Path) -> None:
    """Cartographer is an optional install. Failing here would make it mandatory
    by the back door, and this graph must be fully usable on its own."""
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        report = tools.kg_validate(g)
    finally:
        g.close()
    assert [i["severity"] for i in report["external_link_issues"]] == ["warning"] * 2
    assert report["ok"], "an unverifiable link is not a broken one"


def test_a_resolvable_link_produces_no_finding(tmp_path: Path) -> None:
    _screen_graph(tmp_path / "knowledge" / "cartographer_graph.db", ["rpn-main"])
    g = CodeGraph(_linked_graph(tmp_path))
    try:
        assert tools.kg_validate(g)["external_link_issues"] == []
    finally:
        g.close()


# --- additive, and no version bump --------------------------------------------
def test_a_graph_written_before_the_table_existed_still_opens(tmp_path: Path) -> None:
    """The whole reason this went in without a schema bump. A reader that
    required the table would refuse every graph committed before the update."""
    db = tmp_path / "code_graph.db"
    build(db, Meta(codebase="x", root="src"), [Node(id="a", kind="K", description="One.")])
    con = sqlite3.connect(db)
    con.execute("DROP TABLE external_link")
    con.commit()
    con.close()

    g = CodeGraph(db)
    try:
        assert g.node("a").links == [], "absent reads as 'no links', never as an error"
        assert g.external_links() == []
        assert g.nodes_linking_to("anything#at-all") == []
        assert tools.kg_validate(g)["external_link_issues"] == []
    finally:
        g.close()


def test_upgrading_carries_links_through(tmp_path: Path) -> None:
    """An upgrade that dropped these rows would sever every cross-graph link in
    the file, and nothing downstream could tell it had happened."""
    db = _linked_graph(tmp_path)
    _, _, nodes, _ = upgrade.read_any_version(db)
    assert nodes[0].links == [ExternalLink("cartographer_graph.db#rpn-main", "presented-by")]
    assert upgrade.main([str(db), "--force"]) == 0
    g = CodeGraph(db)
    try:
        assert g.node("rpn_screen").links == nodes[0].links
    finally:
        g.close()


def test_the_parity_constraints_are_untouched(tmp_path: Path) -> None:
    """The reason this table exists is that `counterpart` physically cannot hold
    a link to a screen. Weakening it to fit would have destroyed the invariant it
    enforces, so the additive table must leave every parity rule exactly as it was.
    """
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    refused = [
        # matched must say who
        {"parity": "matched"},
        # divergent must say who and how
        {"parity": "divergent", "counterpart": "p.db#x"},
        # <codebase>-only means there is no peer to point at
        {"parity": "android-only", "counterpart": "p.db#x"},
        # a counterpart link is meaningless without a parity flag
        {"counterpart": "p.db#x"},
        # only a divergence may carry the headline
        {"parity": "matched", "counterpart": "p.db#x", "divergence": "differs"},
    ]
    for i, cols in enumerate(refused):
        cols = {"id": f"n{i}", "kind": "K", **cols}
        sql = (
            f"INSERT INTO node ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})"
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql, tuple(cols.values()))
    con.close()

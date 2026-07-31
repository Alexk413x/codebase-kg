"""Source baselines: what makes a green `kg_validate` mean something.

Checking that an anchor's symbol still exists proves the pointer resolves. It
does not prove the *description* still fits — a refactor that keeps a class name
but guts its behaviour passes the symbol check cleanly. The recorded digest is
what closes that gap, and these tests pin the three behaviours that make it
trustworthy: it notices real edits, it does not re-bless nodes nobody re-read,
and re-baselining is something you have to ask for.
"""

from __future__ import annotations

from pathlib import Path

from codebase_kg import codec, tools
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build


def _repo(tmp_path: Path, body: str = "class Feed { fun rank() {} }") -> tuple[Path, Path]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "Feed.kt").write_text(body, encoding="utf-8")
    return tmp_path, tmp_path / "g.db"


def _meta() -> Meta:
    return Meta(codebase="x", root="", generated="2026-07-30")


def _nodes() -> list[Node]:
    return [Node(id="feed", kind="K", anchors=[Anchor("src/Feed.kt", "Feed")])]


def _validate(db: Path, base: Path) -> dict[str, object]:
    g = CodeGraph(db)
    try:
        return tools.kg_validate(g, None, str(base))
    finally:
        g.close()


# --- the signal --------------------------------------------------------------
def test_unchanged_source_reports_no_drift(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)
    v = _validate(db, base)
    assert v["changed_since_built"] == {"count": 0, "anchors": [], "unhashed": 0}


def test_an_edit_that_keeps_the_symbol_is_still_reported(tmp_path: Path) -> None:
    """The case the symbol check cannot see, which is the entire point."""
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)

    # Same class name, completely different behaviour.
    (base / "src" / "Feed.kt").write_text(
        "class Feed { fun rank() { throw NotImplementedError() } }", encoding="utf-8"
    )

    v = _validate(db, base)
    assert v["anchor_issues"] == []  # symbol still resolves — old check is happy
    changed = v["changed_since_built"]
    assert isinstance(changed, dict)
    assert changed["count"] == 1
    assert "re-read" in changed["anchors"][0]["issue"]  # type: ignore[index]


def test_drift_does_not_make_the_graph_not_ok(tmp_path: Path) -> None:
    """`ok` means broken, not "someone touched a file".

    Folding drift into `ok` would make every graph fail the moment anyone edits
    covered source — the same false-alarm failure mode as the old date-based
    freshness gate this replaced.
    """
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)
    (base / "src" / "Feed.kt").write_text("class Feed { fun rank() { } /* x */ }", encoding="utf-8")
    assert _validate(db, base)["ok"] is True


def test_no_baseline_reads_as_unhashed_not_as_unchanged(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes())  # no source_root -> no baselines
    changed = _validate(db, base)["changed_since_built"]
    assert isinstance(changed, dict)
    assert changed == {"count": 0, "anchors": [], "unhashed": 1}


# --- carry-through -----------------------------------------------------------
def test_a_rebuild_does_not_silently_rebless_unverified_nodes(tmp_path: Path) -> None:
    """The honesty property of the refresh loop.

    export -> edit one node -> build must not quietly re-baseline the nodes the
    author never looked at. If it did, a single refresh would erase every
    outstanding drift signal in the graph.
    """
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)
    (base / "src" / "Feed.kt").write_text("class Feed { fun rank() { TODO() } }", encoding="utf-8")

    # A full export -> build cycle, as /codebase-kg:refresh performs it.
    g = CodeGraph(db)
    try:
        doc = codec.to_dict(g.meta, g.all_nodes(), g.sources())
    finally:
        g.close()
    meta, nodes, sources = codec.from_dict(doc)
    build(db, meta, nodes, source_root=base, sources=sources)

    changed = _validate(db, base)["changed_since_built"]
    assert isinstance(changed, dict)
    assert changed["count"] == 1, "the rebuild swallowed a drift signal"


def test_rebaseline_is_explicit(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)
    (base / "src" / "Feed.kt").write_text("class Feed { fun rank() { TODO() } }", encoding="utf-8")

    build(db, _meta(), _nodes(), source_root=base, rebaseline=True)
    changed = _validate(db, base)["changed_since_built"]
    assert isinstance(changed, dict)
    assert changed["count"] == 0


def test_a_baseline_for_a_dropped_anchor_does_not_ride_along(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    (base / "src" / "Old.kt").write_text("class Old", encoding="utf-8")
    nodes = [
        Node(
            id="feed",
            kind="K",
            anchors=[Anchor("src/Feed.kt", "Feed"), Anchor("src/Old.kt", "Old")],
        )
    ]
    build(db, _meta(), nodes, source_root=base)

    g = CodeGraph(db)
    try:
        assert set(g.sources()) == {"src/Feed.kt", "src/Old.kt"}
        carried = g.sources()
    finally:
        g.close()

    build(db, _meta(), _nodes(), source_root=base, sources=carried)
    g = CodeGraph(db)
    try:
        assert set(g.sources()) == {"src/Feed.kt"}
    finally:
        g.close()


# --- determinism -------------------------------------------------------------
def test_hashing_keeps_the_build_byte_identical(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    build(db, _meta(), _nodes(), source_root=base)
    first = db.read_bytes()
    build(db, _meta(), _nodes(), source_root=base)
    assert db.read_bytes() == first

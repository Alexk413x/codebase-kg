"""A `covers` declaration outranks the ignored-directory defaults.

`IGNORE_DIRS` is a guess at what is never source. `covers` is the repo saying
what is. When they disagree the declaration has to win, and it did not: the
coverage walk pruned `.githooks/` before anything classified it, so files that
the graph declared *and* anchored counted as neither `covered` nor `gap`. They
were simply gone — the fifth, invisible bucket that coverage.py opens by
promising does not exist.

The pre-push gate had the same inversion in `is_source`, one line above a
docstring saying the declaration wins outright.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codebase_kg.coverage import declared_roots
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.tools import coverage_report, walk_sources
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "git-hooks"))

import kg_pre_push  # noqa: E402


# --- which directories a pattern names ---------------------------------------
@pytest.mark.parametrize("pattern, expected", [
    (".githooks/*", {".githooks"}),
    (".githooks/", {".githooks"}),
    ("app/src/**/*.kt", {"app", "app/src"}),
    ("hooks/kg_post_edit_check.py", {"hooks"}),
    ("**/*.py", set()),          # leading wildcard must not un-prune .venv
    ("*.kt", set()),
    ("", set()),
    ("# a comment", set()),
])
def test_declared_roots(pattern: str, expected: set[str]) -> None:
    assert declared_roots([pattern]) == expected


def test_declared_roots_unions_and_includes_ancestors() -> None:
    # `app/` must survive the prune for the walk to ever reach `app/.generated/`.
    assert declared_roots(["app/.generated/*", ".githooks/*"]) == {
        "app", "app/.generated", ".githooks",
    }


# --- the walk ----------------------------------------------------------------
@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in (
        "src/Main.kt",
        ".githooks/pre-push",
        ".githooks/kg_pre_push.py",
        ".github/workflows/ci.yml",
        "node_modules/dep/index.js",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


def test_walk_prunes_ignored_dirs_by_default(tree: Path) -> None:
    assert walk_sources(tree) == ["src/Main.kt"]


def test_walk_keeps_a_declared_dir(tree: Path) -> None:
    files = walk_sources(tree, keep=declared_roots([".githooks/*"]))
    assert files == [".githooks/kg_pre_push.py", ".githooks/pre-push", "src/Main.kt"]
    # Declaring one ignored directory must not readmit the others.
    assert not any(f.startswith((".github/", "node_modules/")) for f in files)


def test_walk_keep_is_case_insensitive_like_the_prune(tree: Path) -> None:
    assert ".githooks/pre-push" in walk_sources(tree, keep={".GitHooks"})


# --- the number the report prints --------------------------------------------
def test_declared_and_anchored_files_count_as_covered(tmp_path: Path) -> None:
    """The regression, as the count that exposed it.

    Two files under `.githooks/`, both declared by `covers`, both anchored. The
    walk used to drop them before `classify` ran, so `covered` under-reported by
    exactly the number of anchored files hiding in an ignored directory.
    """
    repo = tmp_path / "repo"
    for rel in ("src/Main.kt", ".githooks/pre-push", ".githooks/kg_pre_push.py"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    db = tmp_path / "code_graph.db"
    meta = Meta(
        codebase="test",
        root="",
        generated="2026-07-31",
        covers=["src/*.kt", ".githooks/*"],
    )
    nodes = [
        Node(
            id="main", kind="Entry", description="The entry point.",
            anchors=[Anchor("src/Main.kt", "main")], edges=[], section="APP",
        ),
        Node(
            id="push_gate", kind="Hook",
            description="Blocks a push whose graph is behind the source.",
            anchors=[
                Anchor(".githooks/pre-push", "main"),
                Anchor(".githooks/kg_pre_push.py", "is_source"),
            ],
            edges=[], section="TOOLING",
        ),
    ]
    build(db, meta, nodes, source_root=repo)

    graph = CodeGraph(db)
    try:
        report = coverage_report(graph, repo)
    finally:
        graph.close()

    assert report.declared is True
    assert report.covered == 3          # was 1: the two .githooks files vanished
    assert report.gaps == []
    assert report.out_of_scope == 0


def test_an_undeclared_ignored_dir_stays_out_of_scope(tmp_path: Path) -> None:
    """The prune still does its job when nothing declares otherwise."""
    repo = tmp_path / "repo"
    for rel in ("src/Main.kt", ".github/workflows/ci.yml"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="t", root="", generated="2026-07-31", covers=["src/*.kt"]),
        [Node(
            id="main", kind="Entry", description="The entry point.",
            anchors=[Anchor("src/Main.kt", "main")], edges=[], section="APP",
        )],
        source_root=repo,
    )
    graph = CodeGraph(db)
    try:
        report = coverage_report(graph, repo)
    finally:
        graph.close()
    assert report.covered == 1
    assert report.out_of_scope == 0  # .github never entered the walk at all


# --- the same rule in the vendored push gate ---------------------------------
def test_pre_push_lets_covers_outrank_ignore_dirs() -> None:
    covers = kg_pre_push.compile_patterns([".githooks/*"])
    exempt = kg_pre_push.compile_patterns([])
    assert kg_pre_push.is_source(
        ".githooks/pre-push", root="", graph_rel=None, covers=covers, exempt=exempt
    )


def test_pre_push_still_ignores_what_covers_does_not_name() -> None:
    covers = kg_pre_push.compile_patterns([".githooks/*"])
    exempt = kg_pre_push.compile_patterns([])
    assert not kg_pre_push.is_source(
        ".github/workflows/ci.yml", root="", graph_rel=None,
        covers=covers, exempt=exempt,
    )


def test_pre_push_ignore_dirs_still_apply_without_a_declaration() -> None:
    # No `covers` → the extension deny-list and IGNORE_DIRS are all there is.
    assert not kg_pre_push.is_source(".githooks/pre-push", root="", graph_rel=None)
    assert kg_pre_push.is_source("src/Main.kt", root="", graph_rel=None)

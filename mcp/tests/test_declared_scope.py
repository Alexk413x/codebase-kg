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
from codebase_kg.coverage import classify, dir_may_hold
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.tools import _IGNORE_LOWER, coverage_report, walk_sources
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "git-hooks"))

import kg_pre_push


# --- which directories a pattern can reach ----------------------------------
@pytest.mark.parametrize("pattern, rel_dir, expected", [
    (".githooks/*", ".githooks", True),
    (".githooks/", ".githooks", True),
    ("app/.generated/*", "app/.generated", True),
    ("hooks/kg_post_edit_check.py", "hooks", True),
    ("app/src/**/*.kt", "app/src/main/kotlin/com/acme/build", True),
    ("app/src/**/*.kt", "app/build", False),
    ("app/src/**/*.kt", "other/src/build", False),
    ("**/build/*.kt", "app/build", True),
    ("**/build/*.kt", "node_modules/pkg/build", False),
    ("**/*.py", "node_modules", False),    # leading wildcard must not un-prune .venv
    ("**/*.py", ".venv/lib", False),
    ("**/*", "node_modules", False),
    ("*/src/*.kt", "node_modules/src", False),
    ("**", "node_modules", False),
    ("*.kt", "build", False),
    ("", "build", False),
    ("# a comment", "build", False),
    ("build/x.py", "build/x.py", False),   # a file name is not a directory
])
def test_dir_may_hold(pattern: str, rel_dir: str, expected: bool) -> None:
    assert dir_may_hold(rel_dir, [pattern], _IGNORE_LOWER) is expected


def test_dir_may_hold_any_pattern_admits() -> None:
    patterns = ["**/*.py", ".githooks/*"]
    assert dir_may_hold(".githooks", patterns, _IGNORE_LOWER)
    assert not dir_may_hold("node_modules", patterns, _IGNORE_LOWER)


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
    files = walk_sources(tree, covers=[".githooks/*"])
    assert files == [".githooks/kg_pre_push.py", ".githooks/pre-push", "src/Main.kt"]
    # Declaring one ignored directory must not readmit the others.
    assert not any(f.startswith((".github/", "node_modules/")) for f in files)


def test_walk_keeps_an_ignored_dir_a_wildcard_pattern_reaches(tmp_path: Path) -> None:
    for rel in (
        "app/src/main/kotlin/com/acme/build/Config.kt",
        "app/src/main/kotlin/com/acme/Main.kt",
        "app/build/Generated.kt",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    files = walk_sources(tmp_path, covers=["app/src/**/*.kt"])
    assert "app/src/main/kotlin/com/acme/build/Config.kt" in files
    assert "app/build/Generated.kt" not in files
    assert "app/build/Generated.kt" not in walk_sources(tmp_path)


def test_walk_does_not_descend_into_noise_for_a_leading_wildcard(tmp_path: Path) -> None:
    for rel in ("src/a.py", "node_modules/dep/b.py", ".venv/lib/c.py", "x/vendor/d.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    assert walk_sources(tmp_path, covers=["**/*.py"]) == ["src/a.py"]


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


def test_declaring_an_ignored_dir_does_not_change_out_of_scope(tmp_path: Path) -> None:
    """`covered` rises alone. That is the whole signature of the fix.

    The expectation when this was reported was `110/184 → 112/182`, which
    misreads the bug: `classify` computes `out_of_scope` as
    `len(files) - len(in_scope)`, and a pruned file is missing from *both*
    terms, so it never counted as out-of-scope to begin with. Honouring the
    declaration adds it to `files` and to `in_scope` alike, leaving the
    difference untouched. Pinned here because a future change that moves
    `out_of_scope` when a declaration is added is a bug, not an improvement.
    """
    repo = tmp_path / "repo"
    for rel in ("src/Main.kt", "README.md", ".githooks/pre-push"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    anchored = {"src/Main.kt", ".githooks/pre-push"}
    covers = ["src/*.kt", ".githooks/*"]

    # README.md is the only genuinely out-of-scope file, in both walks.
    without = classify(walk_sources(repo), anchored, covers, [])
    with_declared = classify(
        walk_sources(repo, covers=covers), anchored, covers, []
    )

    assert without.covered == 1 and with_declared.covered == 2
    assert without.out_of_scope == with_declared.out_of_scope == 1


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


def test_gap_count_counts_every_gap_past_the_listing_cap() -> None:
    files = [f"src/F{i}.kt" for i in range(60)]
    report = classify(files, set(), ["src/*.kt"], [], limit=50)
    assert len(report.gaps) == 50
    assert report.truncated
    assert report.to_dict()["gap_count"] == 60


# --- a `covers` match wins over the prune at any depth -----------------------
def test_pattern_covered_file_in_a_nested_build_dir_is_covered(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = "app/src/main/kotlin/com/acme/build/Config.kt"
    for rel in ("app/src/main/kotlin/com/acme/Main.kt", nested, "app/build/Skip.kt"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="t", root="", generated="2026-07-31", covers=["app/src/**/*.kt"]),
        [Node(
            id="config", kind="Entry", description="The build configuration.",
            anchors=[Anchor(nested, "Config")], edges=[], section="APP",
        )],
        source_root=repo,
    )
    graph = CodeGraph(db)
    try:
        report = coverage_report(graph, repo)
    finally:
        graph.close()
    assert report.covered == 1
    assert report.gaps == ["app/src/main/kotlin/com/acme/Main.kt"]
    assert report.out_of_scope == 0


def test_an_unanchored_nested_build_file_is_a_gap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rel = "app/src/main/kotlin/com/acme/build/Config.kt"
    p = repo / rel
    p.parent.mkdir(parents=True)
    p.write_text("x", encoding="utf-8")
    files = walk_sources(repo, covers=["app/src/**/*.kt"])
    assert classify(files, set(), ["app/src/**/*.kt"], []).gaps == [rel]


def test_pre_push_lets_covers_outrank_a_nested_ignored_dir() -> None:
    covers = kg_pre_push.compile_patterns(["app/src/**/*.kt"])
    exempt = kg_pre_push.compile_patterns([])
    nested = "app/src/main/kotlin/com/acme/build/Config.kt"
    assert kg_pre_push.is_source(nested, root="", graph_rel=None, covers=covers, exempt=exempt)
    assert not kg_pre_push.is_source(
        "app/build/Skip.kt", root="", graph_rel=None, covers=covers, exempt=exempt
    )
    assert not kg_pre_push.is_source(nested, root="", graph_rel=None)

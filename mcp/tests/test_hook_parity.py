"""The two vendored hook files must agree, and nothing else was checking that.

`git-hooks/kg_pre_push.py` and `hooks/_config.py` are deliberately stdlib-only
and deliberately do **not** import `codebase_kg` — they get copied into repos
that have no plugin install. That constraint forces the same helpers to exist
twice; it does not license them to disagree.

They had already drifted: `hooks/_config.py` was missing five entries from
`IGNORE_DIRS` and one from the extension list, so the post-edit hook nudged
about files in `Pods/` and `node_modules/` that both other surfaces correctly
ignored. Nothing failed — the only thing holding them together was a comment
saying "match the pre-push gate's logic".

These tests are that comment, made executable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "git-hooks"))
sys.path.insert(0, str(ROOT / "hooks"))

import _config  # noqa: E402
import kg_post_edit_check  # noqa: E402
import kg_pre_push  # noqa: E402
from codebase_kg import writer

# DEFAULTS is dict[str, object]; the extension list is the one entry
# tests need as a real list.
EXCLUDE_EXT = cast("list[str]", _config.DEFAULTS["exclude_ext"])


# --- the shared constants ----------------------------------------------------
def test_ignore_dirs_are_identical() -> None:
    assert _config.IGNORE_DIRS == kg_pre_push.IGNORE_DIRS


def test_ignore_dirs_match_the_package_copy_too() -> None:
    """There are *three* copies, and only two were being compared.

    `tools.py` carries its own `IGNORE_DIRS` for the coverage walk, with a
    comment claiming it "mirrors the pre-push gate's list" — the same comment
    that was the only thing holding the other two together, right up until they
    drifted. The third copy was outside the net entirely.
    """
    from codebase_kg import tools

    assert tools.IGNORE_DIRS == kg_pre_push.IGNORE_DIRS
    assert tools._IGNORE_LOWER == {d.lower() for d in tools.IGNORE_DIRS}


def test_excluded_extensions_are_identical() -> None:
    assert set(EXCLUDE_EXT) == kg_pre_push.EXCLUDE_EXT


def test_ignore_dirs_case_folding_is_precomputed_in_both() -> None:
    # The set is NOT all-lowercase (`Pods`, `DerivedData`), so both files must
    # fold case when comparing — comparing raw would silently stop ignoring
    # exactly the vendored directories that motivated adding them.
    assert _config._IGNORE_LOWER == kg_pre_push._IGNORE_LOWER
    assert _config._IGNORE_LOWER == {d.lower() for d in _config.IGNORE_DIRS}


def test_default_graph_path_matches() -> None:
    assert _config.DEFAULTS["graph_path"] == kg_pre_push.DEFAULT_GRAPH


# --- the shared frontmatter rules --------------------------------------------
LOCAL_MD = """---
codebase: android
root: app/src/main   # java only
graph_path: knowledge/code_graph.db
counterpart: <peer path>
language: kotlin
---

Prose below the frontmatter is ignored.
"""


@pytest.mark.parametrize("key, expected", [
    ("codebase", "android"),
    ("root", "app/src/main"),      # the inline comment must be stripped
    ("graph_path", "knowledge/code_graph.db"),
    ("language", "kotlin"),
])
def test_both_parsers_agree_on_values(key: str, expected: str) -> None:
    assert _config._parse_frontmatter(LOCAL_MD)[key] == expected
    assert kg_pre_push._parse_frontmatter(LOCAL_MD)[key] == expected


def test_both_parsers_reject_a_placeholder() -> None:
    # `counterpart: <peer path>` is the template's "not set" spelling.
    assert "counterpart" not in _config._parse_frontmatter(LOCAL_MD)
    assert "counterpart" not in kg_pre_push._parse_frontmatter(LOCAL_MD)


def test_a_comment_tail_does_not_silently_disable_the_hook() -> None:
    # The concrete regression: an un-stripped comment made `root` match no file,
    # so is_source_file returned False for everything and the hook went quiet.
    root = _config._parse_frontmatter(LOCAL_MD)["root"]
    assert "#" not in str(root)


# --- the copied glob matcher -------------------------------------------------
# `kg_pre_push` carries a verbatim copy of coverage.py's pattern helpers, for
# the same reason as everything above: it runs in repos with no plugin install.
# Drift here would mean the push hook and `kg_validate` disagree about which
# files are in scope — silently, and in the direction of saying nothing.
GLOB_CASES = [
    ("app/src/**/*.kt", "app/src/Main.kt", True),
    ("app/src/**/*.kt", "app/src/a/b/Main.kt", True),
    ("app/src/**/*.kt", "app/other/Main.kt", False),
    ("app/*.kt", "app/a/b/Main.kt", False),          # * must not cross a '/'
    ("**/*.gradle.kts", "build.gradle.kts", True),
    ("**/*.gradle.kts", "app/build.gradle.kts", True),
    ("res/", "res/values/strings.xml", True),        # trailing / means "beneath"
    ("res/", "resources/x.xml", False),
    ("?.kt", "A.kt", True),
    ("?.kt", "AB.kt", False),
    ("a+b/*.kt", "a+b/X.kt", True),                  # regex metachars stay literal
]


@pytest.mark.parametrize("pattern, path, expected", GLOB_CASES)
def test_both_glob_matchers_agree(pattern: str, path: str, expected: bool) -> None:
    from codebase_kg import coverage

    pkg = coverage.matches_any(path, coverage.compile_patterns([pattern]))
    hook = kg_pre_push.matches_any(path, kg_pre_push.compile_patterns([pattern]))
    assert pkg == hook == expected


def test_copied_helpers_are_textually_identical() -> None:
    """Behavioural cases cover what someone thought to test; this covers the rest.

    Comparing the source text catches drift in a branch no case exercises, which
    is precisely how `IGNORE_DIRS` drifted before anyone noticed.
    """
    import inspect

    from codebase_kg import coverage

    for name in ("glob_to_regex", "compile_patterns", "matches_any", "parse_patterns"):
        pkg_src = inspect.getsource(getattr(coverage, name))
        hook_src = inspect.getsource(getattr(kg_pre_push, name))
        assert _body(pkg_src) == _body(hook_src), f"{name} has drifted"


def _body(src: str) -> str:
    """The function's executable logic, normalized.

    Parsed and re-emitted rather than diffed as text: the two copies legitimately
    differ in docstring wording, comments, and type annotations (the package
    version is annotated, the vendored one cannot rely on
    `from __future__ import annotations` in an arbitrary repo's Python). None of
    that is drift. Round-tripping through the AST — minus the docstring and the
    signature — leaves exactly the statements, which is the thing that must match.
    """
    import ast
    import textwrap

    fn = ast.parse(textwrap.dedent(src)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_both_parsers_ignore_a_file_without_frontmatter() -> None:
    body = "graph_path: custom.db\n"
    assert _config._parse_frontmatter(body) == {}
    assert kg_pre_push._parse_frontmatter(body) == {}


# --- the shipped template must survive both parsers --------------------------
def test_the_shipped_template_parses_the_same_way_in_both() -> None:
    text = (ROOT / "templates" / "codebase-kg.local.md.example").read_text(encoding="utf-8")
    mine = _config._parse_frontmatter(text)
    theirs = kg_pre_push._parse_frontmatter(text)
    for key in ("codebase", "root", "graph_path", "counterpart", "language"):
        assert str(mine.get(key, "")) == str(theirs.get(key, "")), key


# --- source classification ---------------------------------------------------
@pytest.mark.parametrize("rel", [
    "app/src/main/Foo.kt",
    "app/src/main/ui/Bar.kt",
])
def test_both_agree_a_source_file_counts(tmp_path: Path, rel: str) -> None:
    cfg: dict[str, object] = {"root": "app/src/main", "exclude_ext": list(EXCLUDE_EXT)}
    assert _config.is_source_file(tmp_path / rel, tmp_path, cfg) is True
    assert kg_pre_push.is_source(rel, "app/src/main", None) is True


@pytest.mark.parametrize("rel", [
    "app/src/main/README.md",
    "app/src/main/build/Generated.kt",
    "app/src/main/Pods/Vendor.swift",
    "app/src/main/node_modules/pkg/index.ts",
])
def test_both_agree_a_non_source_file_does_not(tmp_path: Path, rel: str) -> None:
    cfg: dict[str, object] = {"root": "app/src/main", "exclude_ext": list(EXCLUDE_EXT)}
    assert _config.is_source_file(tmp_path / rel, tmp_path, cfg) is False
    assert kg_pre_push.is_source(rel, "app/src/main", None) is False


# --- `root: "."` -------------------------------------------------------------
# A graph authored with `.` rather than `` disabled BOTH hooks in a real repo,
# and neither reported anything: `.` is truthy, so the guards ran, and it
# prefixes no repo-relative path, so every file was discarded. `kg_validate`
# still called that graph healthy, because the package joins paths where these
# two compare them.
@pytest.mark.parametrize("raw", [".", "./", "/./", "  .  "])
def test_both_hooks_fold_a_dot_root_to_empty(raw: str) -> None:
    assert kg_pre_push.norm_root(raw) == ""
    assert kg_post_edit_check.norm_root(raw) == ""


@pytest.mark.parametrize("raw", ["app/src/main", "app/src/main/", "\\app\\src\\main"])
def test_both_hooks_normalize_a_real_root_the_same_way(raw: str) -> None:
    assert kg_pre_push.norm_root(raw) == "app/src/main"
    assert kg_post_edit_check.norm_root(raw) == "app/src/main"


def test_norm_root_has_not_drifted_between_the_copies() -> None:
    import inspect

    assert _body(inspect.getsource(kg_pre_push.norm_root)) == _body(
        inspect.getsource(kg_post_edit_check.norm_root)
    )


@pytest.mark.parametrize("root", ["", "."])
def test_a_dot_root_classifies_source_exactly_like_an_empty_one(
    tmp_path: Path, root: str
) -> None:
    rel = "app/Foo.kt"
    cfg: dict[str, object] = {"root": root, "exclude_ext": list(EXCLUDE_EXT)}
    assert _config.is_source_file(tmp_path / rel, tmp_path, cfg) is True
    assert kg_pre_push.is_source(rel, kg_pre_push.norm_root(root), None) is True
    assert kg_pre_push._rel_to_root(rel, root) == rel


def test_a_dot_root_still_anchors_the_edited_file(tmp_path: Path) -> None:
    """The signal that went missing: an unanchored file must still be reportable."""
    edited = tmp_path / "app" / "Foo.kt"
    assert kg_post_edit_check._rel_to_root(edited, tmp_path, ".") == "app/Foo.kt"


def test_the_meta_model_refuses_to_carry_a_dot_root() -> None:
    """Normalized on construction, so no newly built graph can reintroduce it."""
    from codebase_kg.models import Meta

    assert Meta(codebase="x", root=".").root == ""
    assert Meta(codebase="x", root="app/src").root == "app/src"


# --- the content digest ------------------------------------------------------
# The builder hashes the working tree and the hooks hash the git blob. Those are
# the same bytes only when the checkout already uses LF. On Windows with
# `text=auto eol=lf` every text file differed, so `changed_since_built` fired on
# all of them and the signal meant nothing. Both sides fold CRLF now, and these
# tests hold the two copies together the way the coverage block is held.
def test_content_sha_is_identical_in_both_copies() -> None:
    payload = b"class Known {\r\n    fun rank() = 1\r\n}\r\n"
    assert kg_pre_push.content_sha(payload) == writer.content_sha(payload)


@pytest.mark.parametrize(
    "data",
    [
        b"a\r\nb\r\n",
        b"a\nb\n",
        b"",
        b"no newline at all",
        b"mixed\r\nendings\nhere\r\n",
        b"\x00\x01binary\r\nbytes",
    ],
)
def test_both_copies_agree_on_every_shape(data: bytes) -> None:
    assert kg_pre_push.content_sha(data) == writer.content_sha(data)


def test_crlf_and_lf_hash_the_same() -> None:
    """The whole point: a checkout's line endings are not a change to the code,
    and the two readers of this digest never see the same ones."""
    assert writer.content_sha(b"a\r\nb\r\n") == writer.content_sha(b"a\nb\n")


def test_a_real_content_change_still_differs() -> None:
    """Normalizing line endings must not normalize away the signal."""
    assert writer.content_sha(b"a\r\nb\r\n") != writer.content_sha(b"a\r\nc\r\n")


def test_file_sha_folds_crlf_too(tmp_path) -> None:
    crlf = tmp_path / "crlf.txt"
    lf = tmp_path / "lf.txt"
    crlf.write_bytes(b"one\r\ntwo\r\n")
    lf.write_bytes(b"one\ntwo\n")
    assert writer.file_sha(crlf) == writer.file_sha(lf)


def test_file_sha_of_an_unreadable_path_is_none(tmp_path) -> None:
    assert writer.file_sha(tmp_path / "nope.txt") is None


# --- the staleness comparison ------------------------------------------------
# `kg_stats`, `kg_validate` and both hooks all report drift. The package copy is
# `codebase_kg/staleness.py`; the hook carries the same rule because it is
# vendored into repos with no plugin install. If these disagree, the number an
# agent reads and the number a push blocks on are different numbers — which is
# precisely the confusion the one-helper rule was introduced to end.
def test_classify_has_not_drifted_between_the_copies() -> None:
    import inspect

    from codebase_kg import staleness

    assert _body(inspect.getsource(staleness.classify)) == _body(
        inspect.getsource(kg_pre_push.classify)
    )


def test_the_split_carries_the_same_fields_in_the_same_order() -> None:
    from codebase_kg import staleness

    assert kg_pre_push.Staleness._fields == staleness.Staleness._fields


CLASSIFY_CASES = [
    # (baselines, current, expected stale / unbaselined / unreadable)
    ({"a.kt": "1" * 64}, {"a.kt": "2" * 64}, (["a.kt"], [], [])),
    ({"a.kt": "1" * 64}, {"a.kt": "1" * 64}, ([], [], [])),
    ({}, {"a.kt": "2" * 64}, ([], ["a.kt"], [])),
    ({"a.kt": "1" * 64}, {}, ([], [], ["a.kt"])),
    ({}, {}, ([], [], ["a.kt"])),
]


@pytest.mark.parametrize("baselines, current, expected", CLASSIFY_CASES)
def test_both_copies_split_the_same_way(
    baselines: dict[str, str], current: dict[str, str], expected: tuple[list[str], ...]
) -> None:
    from codebase_kg import staleness

    assert staleness.classify(["a.kt"], baselines, current) == expected
    assert kg_pre_push.classify(["a.kt"], baselines, current) == expected

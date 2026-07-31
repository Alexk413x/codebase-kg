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
import kg_pre_push  # noqa: E402

# DEFAULTS is dict[str, object]; the extension list is the one entry
# tests need as a real list.
EXCLUDE_EXT = cast("list[str]", _config.DEFAULTS["exclude_ext"])


# --- the shared constants ----------------------------------------------------
def test_ignore_dirs_are_identical() -> None:
    assert _config.IGNORE_DIRS == kg_pre_push.IGNORE_DIRS


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

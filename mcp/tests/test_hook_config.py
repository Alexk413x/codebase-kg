"""`hooks/_config.py` — the resolution layer under the post-edit nudge.

`test_hook_parity.py` covers the constants and the frontmatter parser, because
those are the halves that had drifted from the pre-push gate. Everything the
hook actually *calls* at runtime — `load_config`, `find_graph`, `_rows`,
`graph_meta`, `is_anchored`, `is_source_file` — was uncovered, which is how a
file that runs after every edit sat at 64%.

The theme throughout is that this module is best-effort by contract: every
failure has to read as "no nudge", never as an exception, because the caller is
a hook that must not break an edit.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import _config as c  # noqa: E402


@pytest.fixture
def graph_db(tmp_path: Path) -> Path:
    db = tmp_path / "knowledge" / "code_graph.db"
    db.parent.mkdir(parents=True)
    build(
        db,
        Meta(codebase="test", root="app/src", generated="2026-08-01"),
        [Node(
            id="feed_ranker", kind="Domain", description="Ranks the feed.",
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
    )
    return db


# --- project_dir -------------------------------------------------------------
def test_project_dir_prefers_the_claude_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert c.project_dir("/somewhere/else") == tmp_path.resolve()


def test_project_dir_falls_back_to_cwd_then_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert c.project_dir(str(tmp_path)) == tmp_path.resolve()
    assert c.project_dir(None) == Path(".").resolve()


# --- load_config -------------------------------------------------------------
def test_load_config_returns_defaults_without_a_local_md(tmp_path: Path) -> None:
    assert c.load_config(tmp_path) == c.DEFAULTS


def test_load_config_overlays_only_the_declared_keys(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\nroot: app/src\nnudge_every: 9\n---\n", encoding="utf-8"
    )
    cfg = c.load_config(tmp_path)
    assert cfg["root"] == "app/src"
    assert cfg["nudge_every"] == 9
    assert cfg["exclude_ext"] == c.DEFAULTS["exclude_ext"]  # untouched


def test_load_config_does_not_mutate_the_shared_defaults(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\nroot: app/src\n---\n", encoding="utf-8"
    )
    c.load_config(tmp_path)
    assert c.DEFAULTS["root"] == ""  # a module-level dict handed out by reference


@pytest.mark.parametrize("body", [
    "no frontmatter at all",
    "",
    "---\nnot: closed properly",
    "---\n\xff\xfe binary-ish\n---\n",
])
def test_an_unparseable_local_md_falls_back_to_defaults(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(body, encoding="utf-8")
    cfg = c.load_config(tmp_path)
    assert cfg["graph_path"] == c.DEFAULTS["graph_path"]


def test_an_unreadable_local_md_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text("---\n", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert c.load_config(tmp_path) == c.DEFAULTS


# --- find_graph --------------------------------------------------------------
def test_find_graph_resolves_the_default_location(graph_db: Path) -> None:
    assert c.find_graph(graph_db.parent.parent, dict(c.DEFAULTS)) == graph_db.resolve()


def test_find_graph_honours_an_explicit_relative_override(
    tmp_path: Path, graph_db: Path
) -> None:
    moved = tmp_path / "elsewhere" / "g.db"
    moved.parent.mkdir()
    moved.write_bytes(graph_db.read_bytes())
    cfg = {**c.DEFAULTS, "graph_path": "elsewhere/g.db"}
    assert c.find_graph(tmp_path, cfg) == moved.resolve()


def test_find_graph_honours_an_absolute_override(tmp_path: Path, graph_db: Path) -> None:
    cfg = {**c.DEFAULTS, "graph_path": str(graph_db)}
    assert c.find_graph(tmp_path / "unrelated", cfg) == graph_db.resolve()


def test_find_graph_still_reads_the_legacy_kg_path_key(
    tmp_path: Path, graph_db: Path
) -> None:
    cfg = {k: v for k, v in c.DEFAULTS.items() if k != "graph_path"}
    cfg["kg_path"] = str(graph_db)
    assert c.find_graph(tmp_path, cfg) == graph_db.resolve()


def test_find_graph_returns_none_when_absent(tmp_path: Path) -> None:
    # No root fallback: a missing file means "this repo has no graph", and the
    # hook no-ops rather than guessing at some other .db in the tree.
    assert c.find_graph(tmp_path, dict(c.DEFAULTS)) is None


# --- _rows / graph_meta / is_anchored ----------------------------------------
def test_graph_meta_reads_a_committed_key(graph_db: Path) -> None:
    assert c.graph_meta(graph_db, "root") == "app/src"


def test_graph_meta_of_an_unknown_key_is_empty(graph_db: Path) -> None:
    assert c.graph_meta(graph_db, "no_such_key") == ""


def test_is_anchored_is_true_only_for_anchored_paths(graph_db: Path) -> None:
    assert c.is_anchored(graph_db, "domain/FeedRanker.kt")
    assert not c.is_anchored(graph_db, "domain/Unmapped.kt")


@pytest.mark.parametrize("make", [
    lambda p: p.write_text("this is not a database", encoding="utf-8"),
    lambda p: p.write_bytes(b"SQLite format 3\x00truncated"),
    lambda p: None,  # does not exist at all
])
def test_a_broken_graph_reads_as_no_rows_rather_than_raising(
    tmp_path: Path, make: object
) -> None:
    db = tmp_path / "broken.db"
    make(db)  # type: ignore[operator]
    assert c.graph_meta(db, "root") == ""
    assert c.is_anchored(db, "anything") is False


def test_rows_does_not_leave_the_connection_open(graph_db: Path) -> None:
    """The hook runs per edit; a leaked handle per edit would be a real leak."""
    for _ in range(50):
        c.graph_meta(graph_db, "root")
    # A leaked read handle on Windows blocks this; on POSIX it would not.
    conn = sqlite3.connect(graph_db)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()


# --- is_source_file ----------------------------------------------------------
def test_a_file_outside_the_project_is_not_source(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.kt"
    assert not c.is_source_file(outside, tmp_path, dict(c.DEFAULTS))


@pytest.mark.parametrize("rel", [
    "node_modules/dep/index.js",
    ".git/COMMIT_EDITMSG",
    "build/generated/X.kt",
    "Pods/Thing.swift",            # case-folded: the set is not all-lowercase
    "DerivedData/Y.swift",
])
def test_ignored_directories_are_not_source(tmp_path: Path, rel: str) -> None:
    assert not c.is_source_file(tmp_path / rel, tmp_path, dict(c.DEFAULTS))


@pytest.mark.parametrize("name", [
    "NOTES.md", "data.json", "uv.lock", "config.yaml", "setup.cfg",
    ".gitignore", ".gitattributes", "rules.pro",
])
def test_docs_and_config_are_not_source(tmp_path: Path, name: str) -> None:
    assert not c.is_source_file(tmp_path / name, tmp_path, dict(c.DEFAULTS))


def test_a_dotfile_extension_is_matched_by_name_not_suffix(tmp_path: Path) -> None:
    # Path(".gitignore").suffix is "", so a naive suffix check would call it
    # source. The pre-push gate has the same carve-out.
    assert not c.is_source_file(tmp_path / ".gitignore", tmp_path, dict(c.DEFAULTS))


def test_code_under_the_root_is_source(tmp_path: Path) -> None:
    cfg = {**c.DEFAULTS, "root": "app/src"}
    assert c.is_source_file(tmp_path / "app" / "src" / "A.kt", tmp_path, cfg)


def test_code_outside_the_root_is_not_source(tmp_path: Path) -> None:
    cfg = {**c.DEFAULTS, "root": "app/src"}
    assert not c.is_source_file(tmp_path / "other" / "A.kt", tmp_path, cfg)


def test_an_empty_root_means_the_whole_project(tmp_path: Path) -> None:
    assert c.is_source_file(tmp_path / "anywhere" / "A.kt", tmp_path, dict(c.DEFAULTS))


def test_is_source_file_is_still_extension_based_not_covers_aware(
    tmp_path: Path,
) -> None:
    """The known remaining inconsistency, pinned so it is a decision not a drift.

    `kg_validate` and the pre-push gate both let a `covers` declaration outrank
    IGNORE_DIRS (see test_declared_scope.py). This function has no access to the
    declaration and still decides by extension, so a repo declaring `.githooks/*`
    gets no nudge for those files. Lower stakes — a missed suggestion rather than
    a wrong count — but it is the last surface with its own notion of "source".
    """
    assert not c.is_source_file(
        tmp_path / ".githooks" / "pre-push", tmp_path, dict(c.DEFAULTS)
    )


# --- hooks.json --------------------------------------------------------------
def _handlers(tool: str, script: str = "kg_search_gate.py") -> list[dict[str, object]]:
    import json

    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    return [
        h for group in config["hooks"]["PreToolUse"] if group["matcher"] == tool
        for h in group["hooks"] if script in str(h["command"])
    ]


def _if_words(tool: str) -> set[str]:
    rules = [str(h["if"]) for h in _handlers(tool)]
    assert all(r.startswith(f"{tool}(") and r.endswith(" *)") for r in rules), rules
    return {r[len(tool) + 1:-3].lower() for r in rules}


def test_the_shell_if_rules_cover_every_search_word() -> None:
    """The gate starts only for commands an `if` rule names, so a search word the
    parser knows but no rule lists would never reach it."""
    import kg_search_gate as gate

    cmdlets = {"select-string", "sls", "get-childitem", "gci"}
    assert _if_words("PowerShell") == gate._SEARCH_WORDS
    assert _if_words("Bash") == (gate._SEARCH_WORDS - cmdlets) | {"sudo"}


def test_every_shell_handler_carries_an_if_rule() -> None:
    for tool in ("Bash", "PowerShell"):
        assert all("if" in h for h in _handlers(tool)), tool


def test_the_push_gate_starts_only_for_git_commands() -> None:
    handlers = _handlers("Bash", "kg_push_gate.py")
    assert [h["if"] for h in handlers] == ["Bash(git *)"]


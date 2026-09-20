"""Server wiring: path resolution, caching, and the tool registration surface."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator

import pytest

from codebase_kg import migrate, server, tools
from codebase_kg.models import Meta, Node
from codebase_kg.writer import build

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_server_state() -> Iterator[None]:
    """The server remembers a resolved path; each test starts from cold."""
    server._graph_path = None
    yield
    server._graph_path = None


def _repo(tmp_path: Path) -> Path:
    """A repo-shaped tree with a built graph at knowledge/code_graph.db."""
    shutil.copytree(FIX / "android", tmp_path / "android")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    migrate.convert(tmp_path / "android" / "KNOWLEDGE_GRAPH.md", knowledge / "code_graph.db")
    return tmp_path


# --- path resolution ---------------------------------------------------------
def test_cli_arg_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    target = repo / "knowledge" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["server", str(target)])
    assert server._resolve_graph_path() == target.resolve()


def test_env_var_used_when_no_cli_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    target = repo / "knowledge" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.setenv("CODEBASE_KG_PATH", str(target))
    assert server._resolve_graph_path() == target.resolve()


def test_walks_up_to_knowledge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    deep = repo / "android" / "src" / "ui"
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    monkeypatch.chdir(deep)
    assert server._resolve_graph_path() == (repo / "knowledge" / "code_graph.db").resolve()


def test_no_repo_root_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A graph sitting loose at the repo root is deliberately not found.
    build(tmp_path / "code_graph.db", Meta(codebase="x"), [Node(id="a", kind="K")])
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert server._resolve_graph_path() is None


# --- .local.md override ------------------------------------------------------
def _write_local(base: Path, body: str) -> None:
    (base / ".claude").mkdir(parents=True, exist_ok=True)
    (base / ".claude" / "codebase-kg.local.md").write_text(body, encoding="utf-8")


def test_local_graph_path_override(tmp_path: Path) -> None:
    build(tmp_path / "custom.db", Meta(codebase="x"), [Node(id="a", kind="K")])
    _write_local(tmp_path, "---\ngraph_path: custom.db\n---\n")
    assert server._local_graph_path(tmp_path) == tmp_path / "custom.db"


def test_legacy_kg_path_key_still_honored(tmp_path: Path) -> None:
    # Existing checkouts configured before the rename must keep working.
    build(tmp_path / "custom.db", Meta(codebase="x"), [Node(id="a", kind="K")])
    _write_local(tmp_path, "---\nkg_path: custom.db\n---\n")
    assert server._local_graph_path(tmp_path) == tmp_path / "custom.db"


def test_graph_path_wins_over_legacy_key(tmp_path: Path) -> None:
    _write_local(tmp_path, "---\nkg_path: old.db\ngraph_path: new.db\n---\n")
    assert server._local_graph_path(tmp_path) == tmp_path / "new.db"


def test_local_placeholder_is_ignored(tmp_path: Path) -> None:
    _write_local(tmp_path, "---\ngraph_path: <path to the db>\n---\n")
    assert server._local_graph_path(tmp_path) is None


def test_local_without_frontmatter_is_ignored(tmp_path: Path) -> None:
    _write_local(tmp_path, "graph_path: custom.db\n")
    assert server._local_graph_path(tmp_path) is None


def test_local_comment_tail_is_stripped(tmp_path: Path) -> None:
    _write_local(tmp_path, "---\ngraph_path: custom.db   # per-clone\n---\n")
    assert server._local_graph_path(tmp_path) == tmp_path / "custom.db"


# --- errors ------------------------------------------------------------------
def test_missing_graph_error_points_at_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="/codebase-kg:build"):
        server._graph_file()


def test_unmigrated_repo_gets_migration_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The three consumer repos will hit this exact path before they migrate;
    # a bare "not found" would be a dead end.
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    shutil.copy(FIX / "android" / "KNOWLEDGE_GRAPH.md", knowledge / "KNOWLEDGE_GRAPH.md")
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="codebase_kg.migrate"):
        server._graph_file()


# --- opening -----------------------------------------------------------------
def test_a_rebuild_while_the_server_runs_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows refuses to replace a file that anyone holds open. Caching the
    # connection across calls would therefore make /codebase-kg:refresh fail to
    # write its own output whenever the MCP server was running.
    repo = _repo(tmp_path)
    db = repo / "knowledge" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["server", str(db)])
    with server._open_graph() as g:
        assert g.counts()["nodes"] == 4
    build(db, Meta(codebase="rebuilt", root="src"), [Node(id="only", kind="K")])
    with server._open_graph() as g:
        assert g.meta.codebase == "rebuilt"


def test_every_call_sees_the_current_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    db = repo / "knowledge" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["server", str(db)])
    build(db, Meta(codebase="second", root="src"), [Node(id="only", kind="K")])
    with server._open_graph() as g:
        assert g.counts()["nodes"] == 1


def test_peer_is_none_without_a_counterpart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "code_graph.db"
    build(db, Meta(codebase="solo", root="src"), [Node(id="a", kind="K")])
    monkeypatch.setattr("sys.argv", ["server", str(db)])
    with server._open_graph() as g, tools.open_peer(g) as peer:
        assert peer is None


def test_peer_resolves_relative_to_the_graph(
    built_fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = built_fixtures / "android" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["server", str(db)])
    with server._open_graph() as g, tools.open_peer(g) as peer:
        assert peer is not None and peer.meta.codebase == "ios"


def test_missing_peer_file_degrades_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "code_graph.db"
    build(db, Meta(codebase="x", root="src", counterpart="../gone/code_graph.db"),
          [Node(id="a", kind="K")])
    monkeypatch.setattr("sys.argv", ["server", str(db)])
    with server._open_graph() as g, tools.open_peer(g) as peer:
        assert peer is None


# --- tool surface ------------------------------------------------------------
def test_every_tool_is_registered_and_no_others() -> None:
    import asyncio

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "kg_search", "kg_node", "kg_neighborhood", "kg_find_by_kind",
        "kg_find_by_path", "kg_find_by_link", "kg_find_by_reference", "kg_parity_gaps", "kg_stats",
        "kg_validate",
        "kg_upsert_node", "kg_delete_node", "kg_add_link", "kg_remove_link",
        "kg_add_reference", "kg_remove_reference",
    }

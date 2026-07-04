from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from codebase_kg import server


@pytest.fixture(autouse=True)
def _clean_server_state(monkeypatch: pytest.MonkeyPatch):
    """Neutralize CLI/env resolution and reset the module-level caches."""
    monkeypatch.setattr(sys, "argv", ["codebase-kg"])
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    server._graph = None
    server._graph_sig = None
    server._peer = None
    server._peer_sig = None
    server._graph_path = None
    yield
    server._graph = None
    server._graph_sig = None
    server._peer = None
    server._peer_sig = None
    server._graph_path = None


def _mini_kg(node_id: str) -> str:
    return (
        "# T — Knowledge Graph\n\n"
        "```\ncodebase: t\nroot: src\nrefreshed: 2026-01-01\n```\n\n"
        "## NODES\n\n### S\n\n"
        f"| id | {node_id} |\n"
        "| kind | Thing |\n"
        "| summary | A thing. |\n"
    )


def test_missing_kg_raises_then_resolves_once_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Session starts with no KG: every call errors — but once /codebase-kg:build
    # creates the file, the next call must pick it up (no server restart).
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        server._get_graph()
    kg = tmp_path / "knowledge" / "KNOWLEDGE_GRAPH.md"
    kg.parent.mkdir()
    kg.write_text(_mini_kg("a"), encoding="utf-8")
    g = server._get_graph()
    assert g.by_id("a") is not None


def test_graph_reloads_when_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kg = tmp_path / "knowledge" / "KNOWLEDGE_GRAPH.md"
    kg.parent.mkdir()
    kg.write_text(_mini_kg("a"), encoding="utf-8")
    monkeypatch.setenv("CODEBASE_KG_PATH", str(kg))
    assert set(server._get_graph().ids) == {"a"}
    # An edit must invalidate the cache — kg_validate after an edit has to see
    # the post-edit graph, not the startup snapshot.
    kg.write_text(_mini_kg("bb"), encoding="utf-8")
    os.utime(kg, (os.stat(kg).st_atime, os.stat(kg).st_mtime + 5))
    assert set(server._get_graph().ids) == {"bb"}


def test_resolve_honors_local_md_kg_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SCHEMA.md §7: a per-clone .claude/codebase-kg.local.md kg_path override
    # applies to the MCP server too, not only the hooks/pre-push gate.
    kg = tmp_path / "docs" / "KG.md"
    kg.parent.mkdir()
    kg.write_text(_mini_kg("a"), encoding="utf-8")
    dot = tmp_path / ".claude"
    dot.mkdir()
    (dot / "codebase-kg.local.md").write_text(
        "---\nkg_path: docs/KG.md   # non-standard clone\n---\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert server._resolve_graph_path() == kg.resolve()
    assert server._get_graph().by_id("a") is not None

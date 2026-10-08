"""The served tool catalog against the fastmcp registrations it is generated from.

The server never imports fastmcp: it serves `catalog.json` and checks
arguments itself (`core.validate`). These tests fail when the catalog and the
registrations in `server.py` differ, or when the core accepts, refuses or
coerces an argument differently from fastmcp's pydantic validation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import core, resolve, server
from fastmcp import Client

ROOT = Path(__file__).resolve().parents[2]


def test_the_catalog_matches_the_registrations() -> None:
    """Regenerate with `uv run --frozen python scripts/gen_catalog.py` from `mcp/`."""
    committed = json.loads((ROOT / "mcp" / "src" / "codebase_kg" / "catalog.json").read_text(encoding="utf-8"))
    assert committed == server.catalog()
    assert core.CATALOG == committed
    assert core.INSTRUCTIONS == server.INSTRUCTIONS


def test_the_catalog_carries_no_fastmcp_metadata() -> None:
    assert all("_meta" not in tool for tool in core.CATALOG["tools"])
    assert "fastmcp" not in json.dumps(core.CATALOG)


@pytest.fixture
def graph(built_fixtures: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "android" / "code_graph.db"
    path.parent.mkdir()
    path.write_bytes((built_fixtures / "android" / "code_graph.db").read_bytes())
    monkeypatch.setattr("sys.argv", ["codebase-kg", str(path)])
    monkeypatch.setattr(server, "_graph_path", None)
    return path


def _reference(name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    result = asyncio.run(run())
    return result.is_error, json.loads(json.dumps(result.structured_content))


def _core(graph: Path, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    result = core.Core(None).call_tool(resolve.Connection(cwd=graph.parent, explicit=graph), name, args)
    return result["isError"], result.get("structuredContent")


NODE = "feed_ranker"
CASES: list[tuple[str, dict[str, Any]]] = [
    *[("kg_neighborhood", {"id": NODE, "depth": depth}) for depth in (
        2, "2", " 2 ", "+2", "2.0", 2.0, 2.5, "2.5", "two", "1e1", "", True, False, None, [2], "1_0",
    )],
    *[("kg_find_by_kind", {"kind": "", "limit": limit}) for limit in (
        0, 1, "3", 3.0, 1000, 1001, "1001", -1, None,
    )],
    ("kg_find_by_kind", {"kind": "", "offset": -1}),
    ("kg_find_by_kind", {"kind": "", "offset": "2"}),
    ("kg_find_by_kind", {}),
    ("kg_find_by_kind", {"kind": 3}),
    ("kg_search", {"query": "feed"}),
    ("kg_search", {"query": "feed", "kind": None}),
    ("kg_search", {"query": "feed", "kind": 3}),
    ("kg_search", {"query": 5}),
    ("kg_search", {"query": None}),
    ("kg_search", {"query": "feed", "extra": 1}),
    ("kg_node", {"id": NODE}),
    ("kg_node", {"id": ["x"]}),
    ("kg_find_by_kind", {"kind": "", "limit": "2"}),
    ("kg_find_by_kind", {"kind": "", "limit": True}),
    ("kg_find_by_reference", {"query": None}),
    *[("kg_delete_node", {"ids": [NODE], "dry_run": flag}) for flag in (
        True, "true", "True", "yes", "on", "t", "y", "1", 1, 1.0, "maybe", 2, None, 0.5,
    )],
    *[("kg_delete_node", {"ids": ["no_such_node"], "dry_run": flag}) for flag in (
        False, "false", "off", "no", "n", "f", "0", 0, 0.0,
    )],
    ("kg_delete_node", {"ids": [NODE, 1]}),
    ("kg_delete_node", {"ids": "x"}),
    ("kg_delete_node", {"ids": [NODE], "cascade_inbound": "yes"}),
    ("kg_upsert_node", {"nodes": [{"kind": "K"}]}),
    ("kg_upsert_node", {"nodes": "x"}),
    ("kg_upsert_node", {"nodes": [1]}),
    ("kg_upsert_node", {"nodes": [{"id": NODE, "section": 5}]}),
    ("kg_upsert_node", {"nodes": [{"id": NODE, "note": "kept", "section": "S"}]}),
    ("kg_upsert_node", {"nodes": [{"id": NODE, "rebaseline": "yes"}]}),
    ("kg_upsert_node", {"nodes": []}),
    ("kg_add_link", {"node_id": NODE, "target": "peer.db#x"}),
    ("kg_add_link", {"node_id": NODE, "target": "peer.db#x", "kind": None}),
    ("kg_add_link", {"node_id": 1, "target": "peer.db#x"}),
    ("kg_add_reference", {"node_id": NODE, "url": "https://example.com", "path": None}),
    ("kg_add_reference", {"node_id": NODE, "url": "https://example.com", "title": 3}),
    ("kg_remove_reference", {"node_id": NODE, "url": "https://example.com", "symbol": None}),
]


@pytest.mark.parametrize(("name", "args"), CASES, ids=[f"{n}-{json.dumps(a)}" for n, a in CASES])
def test_the_core_checks_arguments_as_fastmcp_does(graph: Path, name: str, args: dict[str, Any]) -> None:
    original = graph.read_bytes()
    expected = _reference(name, args)
    graph.write_bytes(original)
    assert _core(graph, name, args) == expected


def test_a_validation_error_names_the_tool_and_argument(graph: Path) -> None:
    result = core.Core(None).call_tool(resolve.Connection(cwd=graph.parent, explicit=graph),
                                       "kg_neighborhood", {"id": NODE, "depth": "two"})
    assert result["isError"] is True and "structuredContent" not in result
    text = result["content"][0]["text"]
    assert text.startswith("1 validation error for call[kg_neighborhood]\ndepth")

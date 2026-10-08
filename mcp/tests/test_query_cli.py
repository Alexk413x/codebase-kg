from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from codebase_kg import query, tools
from codebase_kg.store import CodeGraph

RUNNER = Path(tools.__file__).resolve().parents[2] / "launch" / "kg_cli.py"


def _run(graph: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_KG_PATH"}
    return subprocess.run(
        [sys.executable, "-I", str(RUNNER), "query", "--graph", str(graph), *args], capture_output=True, env=env, check=False
    )


@pytest.fixture
def graph_file(built_fixtures: Path) -> Path:
    return built_fixtures / "android" / "code_graph.db"


def _calls(graph_file: Path) -> list[tuple[str, dict]]:
    g = CodeGraph(graph_file)
    try:
        node = g.all_nodes()[0]
    finally:
        g.close()
    path = node.anchors[0].path if node.anchors else "src"
    return [
        ("kg_search", {"query": node.id}),
        ("kg_node", {"id": node.id}),
        ("kg_neighborhood", {"id": node.id, "depth": 2}),
        ("kg_find_by_kind", {"kind": node.kind}),
        ("kg_find_by_path", {"path": path}),
        ("kg_find_by_link", {"target": "x.db#y"}),
        ("kg_find_by_reference", {}),
        ("kg_parity_gaps", {}),
        ("kg_stats", {}),
        ("kg_validate", {"limit": 5}),
    ]


def test_every_read_tool_prints_what_the_function_returns(graph_file: Path) -> None:
    calls = _calls(graph_file)
    assert sorted({t for t, _ in calls}) == sorted(query.TOOLS)
    for tool, args in calls:
        p = _run(graph_file, tool, json.dumps(args))
        assert p.returncode == 0, (tool, p.stdout, p.stderr)
        g = CodeGraph(graph_file)
        try:
            expected = json.loads(json.dumps(query.TOOLS[tool](g, args)))
        finally:
            g.close()
        assert json.loads(p.stdout.decode("utf-8")) == expected, tool


def test_arguments_on_stdin(graph_file: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_KG_PATH"}
    p = subprocess.run(
        [sys.executable, "-I", str(RUNNER), "query", "--graph", str(graph_file), "kg_stats"],
        input=b"{}", capture_output=True, env=env, check=False
    )
    assert p.returncode == 0
    assert "nodes" in json.loads(p.stdout)


@pytest.mark.parametrize(
    ("args", "error"),
    [("not json", "arguments are not JSON"), ("[]", "must be a JSON object"), ("{}", "missing argument")],
)
def test_bad_arguments_fail_with_json(graph_file: Path, args: str, error: str) -> None:
    p = _run(graph_file, "kg_node", args)
    assert p.returncode == 1
    out = json.loads(p.stdout)
    assert out["ok"] is False and error in out["error"]


def test_missing_graph_fails_with_json(tmp_path: Path) -> None:
    p = _run(tmp_path / "none.db", "kg_stats")
    assert p.returncode == 1
    assert "no code graph" in json.loads(p.stdout)["error"]

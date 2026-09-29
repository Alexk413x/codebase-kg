"""How a skill runs the CLIs from a repo that has the plugin but no install.

`python -m codebase_kg.build` works inside the plugin's own checkout and nowhere
else: a target repo has no importable `codebase_kg`, so the instruction is a
`ModuleNotFoundError` at the moment the skill has finished its real work.

The skills call `mcp/launch/kg_cli.py` by its `${CLAUDE_PLUGIN_ROOT}` path
instead. The CLIs import only the standard library, so the runner needs any
Python and nothing installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg import cli, tools
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

PACKAGE_ROOT = Path(tools.__file__).resolve().parents[2]
RUNNER = PACKAGE_ROOT / "launch" / "kg_cli.py"
DOC = (
    '{"codebase": "smoke", "root": "", "generated": "2026-09-06",'
    ' "nodes": [{"id": "a", "kind": "K", "description": "A thing.",'
    ' "section": "S", "anchors": [], "edges": []}]}'
)


@pytest.fixture
def graph(tmp_path: Path) -> CodeGraph:
    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-09-06"),
        [Node(id="a", kind="K", description="A thing.", anchors=[Anchor("ui/A.kt", "A")])],
    )
    return CodeGraph(db)


def test_every_console_script_is_still_declared() -> None:
    """Git's textconv driver runs `codebase-kg-export` through uvx in every
    consumer repo, so the entry points outlive the skills' use of them."""
    declared = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    scripts = declared.split("[project.scripts]", 1)[1].split("[build-system]", 1)[0]
    for key in ("build", "export", "migrate"):
        assert f"codebase-kg-{key} =" in scripts, f"no entry point for codebase-kg-{key}"


def test_kg_stats_reports_the_graph_and_no_cli_field(graph: CodeGraph) -> None:
    try:
        stats = tools.kg_stats(graph)
    finally:
        graph.close()
    assert stats["codebase"] == "x"
    assert stats["root"] == "src"
    assert stats["nodes"] == 1 and stats["anchors"] == 1
    assert "cli" not in stats


# --- the stdlib runner the skills call -----------------------------------------
@pytest.mark.parametrize("name", ["build", "export", "migrate", "upgrade"])
def test_the_runner_runs_every_cli_without_a_venv(name: str) -> None:
    """The skills call the runner with any Python; the CLIs must need nothing else."""
    proc = subprocess.run(
        [sys.executable, "-I", str(RUNNER), name, "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()


def test_the_runner_refuses_an_unknown_command() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(RUNNER), "serve"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "usage" in proc.stderr


def test_the_runner_builds_a_graph_from_an_unrelated_directory(tmp_path: Path) -> None:
    """End to end, the way a skill runs it: an isolated interpreter, a repo that
    is not the plugin, and a graph at the end."""
    doc = tmp_path / "doc.json"
    doc.write_text(DOC, encoding="utf-8")
    out = tmp_path / "out.db"
    proc = subprocess.run(
        [sys.executable, "-I", str(RUNNER), "build", str(doc), "-o", str(out)],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert CodeGraph(out).counts()["nodes"] == 1


def test_server_errors_print_the_runner_command() -> None:
    assert cli.command("export", "-o x.json") == (
        f'uv run --no-project --quiet "{RUNNER.resolve()}" export -o x.json'
    )


def test_the_module_forms_still_work_inside_the_checkout(tmp_path: Path) -> None:
    """`python -m` is still right inside the plugin's own repo, where its tests run."""
    doc = tmp_path / "doc.json"
    doc.write_text(DOC, encoding="utf-8")
    out = tmp_path / "out.db"
    proc = subprocess.run(
        [sys.executable, "-m", "codebase_kg.build", str(doc), "-o", str(out)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()

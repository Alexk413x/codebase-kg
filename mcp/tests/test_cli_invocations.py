"""How a skill learns to run the CLIs, when it cannot work it out for itself.

Every skill's build step said `python -m codebase_kg.build`. That works inside
the plugin's own checkout and nowhere else: a target repo has the plugin but no
importable `codebase_kg`, so the instruction is a `ModuleNotFoundError` at the
moment the skill has finished its real work. The `uvx --from <plugin>/mcp` form
would work, but a skill cannot build it — `CLAUDE_PLUGIN_ROOT` is not set in the
shell a skill's Bash runs in, and the console scripts are not on PATH.

So `kg_stats` reports the invocation. The server is the one component that knows
where it was loaded from, which makes it the only honest answer.

Two shapes, because the package is delivered two ways: a source checkout (what
the plugin ships) gets `uvx --from`, and a wheel install gets the bare console
script, which is on PATH exactly when that is true.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg import tools
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

PACKAGE_ROOT = Path(tools.__file__).resolve().parents[2]


@pytest.fixture
def graph(tmp_path: Path) -> CodeGraph:
    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-09-06"),
        [Node(id="a", kind="K", description="A thing.", anchors=[Anchor("ui/A.kt", "A")])],
    )
    return CodeGraph(db)


# --- the two shapes ----------------------------------------------------------
def test_a_source_checkout_yields_the_uvx_form() -> None:
    """What the plugin actually ships. The quoted path matters — the plugin
    cache lives under a Windows profile path with spaces in it."""
    cli = tools.cli_invocations()
    assert cli["package_root"] == str(PACKAGE_ROOT)
    for key in ("build", "export", "migrate"):
        assert cli[key].startswith(f'uvx --from "{PACKAGE_ROOT}" codebase-kg-')


def test_a_wheel_install_yields_the_bare_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed into site-packages, `parents[2]` is a lib directory, not
    something `uvx --from` could resolve. The console scripts are on PATH in
    exactly that case, so name them instead of building a broken path."""
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    cli = tools.cli_invocations()
    assert cli == {
        "package_root": "",
        "build": "codebase-kg-build",
        "export": "codebase-kg-export",
        "migrate": "codebase-kg-migrate",
    }


def test_every_named_command_is_a_declared_entry_point() -> None:
    """The invocations are only useful if the scripts exist. Read pyproject
    rather than trusting that someone updated both."""
    declared = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    scripts = declared.split("[project.scripts]", 1)[1].split("[build-system]", 1)[0]
    for key in ("build", "export", "migrate"):
        assert f"codebase-kg-{key} =" in scripts, f"no entry point for codebase-kg-{key}"


# --- it reaches the caller ---------------------------------------------------
def test_kg_stats_carries_the_invocations(graph: CodeGraph) -> None:
    try:
        stats = tools.kg_stats(graph)
    finally:
        graph.close()
    assert set(stats["cli"]) == {"package_root", "build", "export", "migrate"}
    assert stats["cli"]["build"].endswith("codebase-kg-build")


def test_kg_stats_still_reports_the_graph(graph: CodeGraph) -> None:
    """The new key must not have displaced anything — every skill reads these."""
    try:
        stats = tools.kg_stats(graph)
    finally:
        graph.close()
    assert stats["codebase"] == "x"
    assert stats["root"] == "src"
    assert stats["nodes"] == 1 and stats["anchors"] == 1


# --- and it actually runs ----------------------------------------------------
@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx is not installed")
def test_the_reported_build_command_runs_outside_the_plugin(tmp_path: Path) -> None:
    """The whole point, end to end: take what `kg_stats` reports, run it from an
    unrelated directory with no install, and get a graph. A unit test of the
    string would have passed while the instruction stayed unrunnable."""
    doc = tmp_path / "doc.json"
    doc.write_text(
        '{"meta": {"codebase": "smoke", "root": "src", "generated": "2026-09-06"},'
        ' "nodes": [{"id": "a", "kind": "K", "description": "A thing.",'
        ' "section": "S", "anchors": [], "edges": []}]}',
        encoding="utf-8",
    )
    out = tmp_path / "out.db"
    proc = subprocess.run(
        ["uvx", "--from", str(PACKAGE_ROOT), "codebase-kg-build", str(doc), "-o", str(out)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    assert CodeGraph(out).counts()["nodes"] == 1


def test_the_module_forms_still_work_inside_the_checkout(tmp_path: Path) -> None:
    """The old `python -m` invocation is not withdrawn — it is the right one when
    you are working in the plugin's own repo, which is where its tests run."""
    doc = tmp_path / "doc.json"
    doc.write_text(
        '{"meta": {"codebase": "smoke", "root": "", "generated": "2026-09-06"},'
        ' "nodes": [{"id": "a", "kind": "K", "description": "A thing.",'
        ' "section": "S", "anchors": [], "edges": []}]}',
        encoding="utf-8",
    )
    out = tmp_path / "out.db"
    proc = subprocess.run(
        [sys.executable, "-m", "codebase_kg.build", str(doc), "-o", str(out)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()

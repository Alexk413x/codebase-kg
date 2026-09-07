"""How a skill learns to run the CLIs, when it cannot work it out for itself.

Every skill's build step said `python -m codebase_kg.build`. That works inside
the plugin's own checkout and nowhere else: a target repo has the plugin but no
importable `codebase_kg`, so the instruction is a `ModuleNotFoundError` at the
moment the skill has finished its real work. The `uvx --from <plugin>/mcp` form
would work, but a skill cannot build it — `CLAUDE_PLUGIN_ROOT` is not set in the
shell a skill's Bash runs in, and the console scripts are not on PATH.

So `kg_stats` reports the invocation. The server is the one component that knows
where it was loaded from, which makes it the only honest answer.

Three ways the package is reached, and the answer differs for each: the repo
checkout it is developed in, the directory it was installed from (PEP 610's
`direct_url.json`, which is what `uvx --from` leaves behind), and a genuine
index install where only the console scripts exist.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
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


# --- how the root is found ---------------------------------------------------
def test_a_source_checkout_yields_the_uvx_form() -> None:
    """The quoted path matters — the plugin cache lives under a Windows profile
    path with spaces in it."""
    cli = tools.cli_invocations()
    assert cli["package_root"] == str(PACKAGE_ROOT)
    for key in ("build", "export", "migrate"):
        assert cli[key].startswith(f'uvx --from "{PACKAGE_ROOT}" codebase-kg-')


def test_an_installed_package_is_resolved_from_its_own_dist_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration that actually ships, and the one the first version got
    wrong. Under `uvx --from <plugin>/mcp` the package lives in a venv, so
    walking up from `__file__` finds a lib directory and the answer degraded to
    a bare console script — a command the caller has no way to run, since the
    scripts are not on a skill's PATH. PEP 610 records the install source.
    """
    plugin = tmp_path / "plugin" / "mcp"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text("[project]\nname='codebase-kg'\n", encoding="utf-8")
    # No pyproject beside the package: this is the venv layout.
    monkeypatch.setattr(tools, "_checkout_root", lambda: None)
    monkeypatch.setattr(tools, "_installed_from", lambda: plugin)
    assert tools.package_root() == plugin
    assert tools.cli_invocations()["build"] == f'uvx --from "{plugin}" codebase-kg-build'


def test_the_plugin_root_env_var_is_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "mcp").mkdir(parents=True)
    (plugin / "mcp" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_checkout_root", lambda: None)
    monkeypatch.setattr(tools, "_installed_from", lambda: None)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    assert tools.package_root() == plugin / "mcp"


def test_no_resolvable_root_falls_back_to_the_bare_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuinely installed from an index: nothing local to point `--from` at,
    and the console scripts really are on PATH in that case."""
    monkeypatch.setattr(tools, "_checkout_root", lambda: None)
    monkeypatch.setattr(tools, "_installed_from", lambda: None)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert tools.cli_invocations() == {
        "package_root": "",
        "build": "codebase-kg-build",
        "export": "codebase-kg-export",
        "migrate": "codebase-kg-migrate",
    }


def test_a_non_local_install_url_yields_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`direct_url.json` can name a VCS or an index. Neither is a directory
    `uvx --from` could be handed."""
    class _Dist:
        @staticmethod
        def read_text(_name: str) -> str:
            return '{"url": "https://example.invalid/codebase-kg.whl"}'

    monkeypatch.setattr(importlib_metadata, "distribution", lambda _n: _Dist())
    assert tools._installed_from() is None


def test_missing_dist_metadata_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_n: str) -> object:
        raise importlib_metadata.PackageNotFoundError("codebase-kg")

    monkeypatch.setattr(importlib_metadata, "distribution", boom)
    assert tools._installed_from() is None


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
        '{"codebase": "smoke", "root": "src", "generated": "2026-09-06",'
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
        '{"codebase": "smoke", "root": "", "generated": "2026-09-06",'
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

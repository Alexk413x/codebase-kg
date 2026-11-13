"""The Codex SessionStart hook moves the codebase-kg paths in `config.toml` to the installed plugin."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from codebase_kg import codex_config

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import _codex_config as refresher
import kg_session_start as hook


def _plugin(root: Path, version: str) -> Path:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codebase-kg", "version": version}), encoding="utf-8"
    )
    (root / "mcp" / "launch").mkdir(parents=True)
    return root


def _table(root: Path, monkeypatch: pytest.MonkeyPatch, stdio: bool = False) -> str:
    """The table `codex-config` prints for a plugin at `root`."""
    monkeypatch.setattr(codex_config, "LAUNCH", root / "mcp" / "launch")
    return codex_config.stdio_entry() if stdio else codex_config.entry(47821, None)


OTHER = (
    "model = 'gpt-5'\r\n"
    "\r\n"
    "[mcp_servers.other]\r\n"
    "command = 'C:\\tools\\mcp\\launch\\kg-shim.cmd'  # not ours to move\r\n"
    "\r\n"
)


@pytest.mark.parametrize("stdio", [False, True])
@pytest.mark.parametrize("folder", ["cache", "with space", "it's"])
def test_a_table_for_an_older_release_moves_to_the_installed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdio: bool, folder: str
) -> None:
    old = _plugin(tmp_path / folder / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / folder / "0.15.4", "0.15.4")
    before = OTHER + _table(old, monkeypatch, stdio) + "\n[features]\nx = true\n"
    after = refresher.rewritten(before, new)
    assert after is not None
    assert after == OTHER + _table(new, monkeypatch, stdio) + "\n[features]\nx = true\n"
    assert refresher.rewritten(after, new) is None


def test_a_table_for_a_missing_folder_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    after = refresher.rewritten(_table(tmp_path / "gone", monkeypatch), new)
    assert after == _table(new, monkeypatch)


@pytest.mark.parametrize("version", ["0.15.4", "0.16.0"])
def test_a_table_for_a_current_or_newer_checkout_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    checkout = _plugin(tmp_path / "dev", version)
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    assert refresher.rewritten(_table(checkout, monkeypatch), new) is None


def test_the_port_and_home_arguments_survive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _plugin(tmp_path / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    monkeypatch.setattr(codex_config, "LAUNCH", old / "mcp" / "launch")
    before = codex_config.entry(5000, "D:\\kg home")
    after = refresher.rewritten(before, new)
    monkeypatch.setattr(codex_config, "LAUNCH", new / "mcp" / "launch")
    assert after == codex_config.entry(5000, "D:\\kg home")


def test_the_plugin_root_variable_moves_only_for_codebase_kg(tmp_path: Path) -> None:
    old = _plugin(tmp_path / "codebase-kg" / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / "codebase-kg" / "0.15.4", "0.15.4")
    text = "[shell_environment_policy.set]\nCLAUDE_PLUGIN_ROOT = '{}'\n"
    assert refresher.rewritten(text.format(old), new) == text.format(new)
    assert refresher.rewritten(text.format(tmp_path / "another-plugin"), new) is None


def test_refresh_writes_the_file_and_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _plugin(tmp_path / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    home = tmp_path / "codex-home"
    home.mkdir()
    config = home / "config.toml"
    config.write_bytes((OTHER + _table(old, monkeypatch)).encode("utf-8"))
    message = refresher.refresh(new, {"CODEX_HOME": str(home)})
    assert message is not None and str(config) in message and "0.15.4" in message
    assert config.read_bytes() == (OTHER + _table(new, monkeypatch)).encode("utf-8")
    assert refresher.refresh(new, {"CODEX_HOME": str(home)}) is None
    assert [p.name for p in home.iterdir()] == ["config.toml"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_refresh_keeps_the_file_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _plugin(tmp_path / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    home = tmp_path / "codex-home"
    home.mkdir()
    config = home / "config.toml"
    config.write_bytes(_table(old, monkeypatch).encode("utf-8"))
    config.chmod(0o600)
    assert refresher.refresh(new, {"CODEX_HOME": str(home)}) is not None
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_refresh_writes_through_a_symlinked_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _plugin(tmp_path / "0.15.3", "0.15.3")
    new = _plugin(tmp_path / "0.15.4", "0.15.4")
    home = tmp_path / "codex-home"
    home.mkdir()
    real = tmp_path / "dotfiles" / "codex.toml"
    real.parent.mkdir()
    real.write_bytes(_table(old, monkeypatch).encode("utf-8"))
    try:
        os.symlink(real, home / "config.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert refresher.refresh(new, {"CODEX_HOME": str(home)}) is not None
    assert (home / "config.toml").is_symlink()
    assert real.read_bytes() == _table(new, monkeypatch).encode("utf-8")
    assert sorted(p.name for p in real.parent.iterdir()) == ["codex.toml"]


def test_refresh_is_silent_without_a_config(tmp_path: Path) -> None:
    assert refresher.refresh(tmp_path, {"CODEX_HOME": str(tmp_path / "none")}) is None


def test_only_the_codex_hook_refreshes_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude Code runs the same script from its own plugin folder, which is not Codex's."""
    calls: list[Path] = []
    monkeypatch.setattr(hook, "refresh_codex_config", lambda root: calls.append(root) or "moved")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)

    class _Stdin:
        def read(self) -> str:
            return json.dumps({"cwd": str(tmp_path)})

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(sys, "argv", ["kg_session_start.py"])
    hook.main()
    assert calls == [] and capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "argv", ["kg_session_start.py", "--codex"])
    hook.main()
    assert calls == [hook.PLUGIN_ROOT]
    assert json.loads(capsys.readouterr().out) == {"systemMessage": "moved"}


def test_the_codex_hooks_file_passes_the_flag() -> None:
    hooks = json.loads((HOOKS / "codex-hooks.json").read_text(encoding="utf-8"))
    entry = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["command"].endswith("kg_session_start.py\" --codex")
    assert entry["commandWindows"].endswith("kg_session_start.py\" --codex")
    claude = (HOOKS / "hooks.json").read_text(encoding="utf-8")
    assert "--codex" not in claude


def test_skip_kg_also_skips_the_codex_config_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(hook, "refresh_codex_config", lambda root: calls.append(root) or "moved")
    monkeypatch.setenv("SKIP_KG", "1")

    class _Stdin:
        def read(self) -> str:
            return json.dumps({"cwd": str(tmp_path)})

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(sys, "argv", ["kg_session_start.py", "--codex"])
    hook.main()
    assert calls == [] and capsys.readouterr().out == ""

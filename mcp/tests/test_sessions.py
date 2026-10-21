"""Session registration: the registry, the hooks and CLI that write it, and the server exit it drives."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import control, daemon, shim
from test_http_server import BUILD, TIMEOUT, _free_port, _start, _state, _stop

ROOT = Path(__file__).resolve().parents[2]
START_HOOK = ROOT / "hooks" / "kg_server_start.py"
HEADERS = ROOT / "mcp" / "launch" / "kg_headers.py"
END_HOOK = ROOT / "hooks" / "kg_session_end.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --- the registry -------------------------------------------------------------
def test_a_registered_session_is_live_until_it_unregisters(cache: Path) -> None:
    assert shim.register_session("abc-1", os.getpid())
    assert shim.register_session("abc-2", os.getpid())
    assert shim.live_sessions() == ["abc-1", "abc-2"]
    assert shim.unregister_session("abc-1")
    assert shim.live_sessions() == ["abc-2"]
    assert not shim.unregister_session("abc-1")


def test_a_session_whose_agent_exited_is_dropped(cache: Path) -> None:
    shim.register_session("gone", _dead_pid())
    shim.register_session("here", os.getpid())
    assert shim.live_sessions() == ["here"]
    assert not (shim.sessions_dir() / "gone.json").exists()


def test_a_session_with_no_pid_expires_after_the_ttl(cache: Path) -> None:
    shim.register_session("unknown", None)
    assert shim.live_sessions() == ["unknown"]
    (shim.sessions_dir() / "unknown.json").write_text(
        json.dumps({"pid": None, "at": time.time() - shim.SESSION_TTL - 1}), encoding="utf-8")
    assert shim.live_sessions() == []


@pytest.mark.parametrize("session", ["", "../escape", "a/b", "a\\b", ".hidden", "x" * 65])
def test_an_id_that_is_not_a_plain_name_is_refused(cache: Path, session: str) -> None:
    assert not shim.register_session(session, os.getpid())
    assert not shim.unregister_session(session)
    assert not list(cache.rglob("*.json"))


def test_a_damaged_record_does_not_break_the_listing(cache: Path) -> None:
    shim.register_session("fine", os.getpid())
    (shim.sessions_dir() / "bad.json").write_text("{", encoding="utf-8")
    assert shim.live_sessions() == ["fine"]


# --- the agent's pid ----------------------------------------------------------
def test_the_agent_is_the_nearest_ancestor_that_is_not_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {10: (20, "python3"), 20: (30, "py"), 30: (40, "bash"), 40: (50, "claude"), 50: (1, "pwsh")}
    monkeypatch.setattr(shim, "_process_table", lambda: table)
    monkeypatch.setattr(os, "getppid", lambda: 20)
    assert shim.agent_pid() == 40


def test_the_agent_is_unknown_when_the_chain_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shim, "_process_table", lambda: {20: (99, "bash")})
    monkeypatch.setattr(os, "getppid", lambda: 20)
    assert shim.agent_pid() is None
    monkeypatch.setattr(shim, "_process_table", dict)
    assert shim.agent_pid() is None


def test_the_process_table_lists_this_process() -> None:
    table = shim._process_table()
    assert table[os.getpid()][0] == os.getppid()
    assert table[os.getpid()][1].startswith("python")


# --- the hooks and the CLI ----------------------------------------------------
def test_the_start_hook_registers_the_session_with_the_agents_pid(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _load(START_HOOK, "kg_server_start_for_sessions")
    fake = hook._shim()
    monkeypatch.setattr(fake, "agent_pid", lambda: 4242)
    hook.register(fake, json.dumps({"session_id": "sess-9", "cwd": "."}))
    record = json.loads((shim.sessions_dir() / "sess-9.json").read_text(encoding="utf-8"))
    assert record["pid"] == 4242


@pytest.mark.parametrize("raw", ["", "not json", "[]", "{}", '{"session_id": 7}'])
def test_the_start_hook_ignores_a_payload_with_no_session(cache: Path, raw: str) -> None:
    hook = _load(START_HOOK, "kg_server_start_for_bad_payloads")
    hook.register(hook._shim(), raw)
    assert not shim.sessions_dir().exists()


def test_the_end_hook_unregisters_the_session(cache: Path) -> None:
    hook = _load(END_HOOK, "kg_session_end_under_test")
    shim.register_session("sess-9", os.getpid())
    assert hook.unregister(hook._shim(), json.dumps({"session_id": "sess-9", "reason": "other"}))
    assert shim.live_sessions() == []
    assert not hook.unregister(hook._shim(), "not json")


def test_the_end_hook_prints_nothing_and_exits_zero(cache: Path) -> None:
    shim.register_session("sess-9", os.getpid())
    proc = subprocess.run([sys.executable, str(END_HOOK)], input=json.dumps({"session_id": "sess-9"}),
                          capture_output=True, text=True, env={**os.environ}, timeout=TIMEOUT, check=False)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert shim.live_sessions() == []


def test_the_hooks_are_registered() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    script = '"${CLAUDE_PLUGIN_ROOT}/hooks/kg_session_end.py"'
    commands = [h["command"] for entry in hooks["SessionEnd"] for h in entry["hooks"]]
    assert commands == [f"command -v py >/dev/null 2>&1 && py -3 {script} || python3 {script} || python {script}"]
    assert all(h["timeout"] <= 5 for entry in hooks["SessionEnd"] for h in entry["hooks"])


def test_the_headers_helper_registers_a_codex_session_by_its_pid(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _load(HEADERS, "kg_headers_for_sessions")
    fake = helper._shim()
    monkeypatch.setattr(fake, "agent_pid", lambda: os.getpid())
    helper.register_agent(fake)
    assert shim.live_sessions() == [f"codex-{os.getpid()}"]


def test_the_headers_helper_registers_with_no_pid_when_the_chain_is_unreadable(
    cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load(HEADERS, "kg_headers_for_unknown_pid")
    fake = helper._shim()
    monkeypatch.setattr(fake, "agent_pid", lambda: None)
    helper.register_agent(fake)
    assert shim.live_sessions() == ["codex-unknown"]


def test_the_headers_helper_registers_only_when_it_starts_the_server(
    cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load(HEADERS, "kg_headers_for_start_flag")
    monkeypatch.setenv("CODEBASE_KG_PORT", str(_free_port()))
    helper.headers(wait=0.1)
    assert shim.live_sessions() == []


def test_the_cli_registers_lists_and_unregisters(cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert control.main(["register", "--session", "cli-1", "--pid", str(os.getpid())]) == 0
    assert control.main(["sessions"]) == 0
    assert control.main(["unregister", "--session", "cli-1"]) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[1]["sessions"] == ["cli-1"]
    assert out[2]["removed"] is True
    assert shim.live_sessions() == []
    assert control.main(["register", "--session", "../x"]) == 1


# --- when the server exits ----------------------------------------------------
def _daemon(grace: float) -> daemon.Daemon:
    return daemon.Daemon("v", "t", 3600.0, http_port=1, http_timeout=3600.0, session_grace=grace)


def test_a_server_that_never_saw_a_session_does_not_exit_on_the_registry(cache: Path) -> None:
    server = _daemon(0.05)
    time.sleep(0.1)
    assert not server.abandoned()
    assert not server.abandoned()


def test_a_server_exits_after_its_last_session_and_the_grace_period(cache: Path) -> None:
    server = _daemon(0.3)
    shim.register_session("one", os.getpid())
    assert not server.abandoned()
    shim.unregister_session("one")
    assert not server.abandoned()
    time.sleep(0.4)
    assert server.abandoned()


def test_a_session_that_registers_within_the_grace_period_keeps_the_server(cache: Path) -> None:
    server = _daemon(0.3)
    shim.register_session("one", os.getpid())
    server.abandoned()
    shim.unregister_session("one")
    server.abandoned()
    time.sleep(0.2)
    shim.register_session("two", os.getpid())
    assert not server.abandoned()
    shim.unregister_session("two")
    assert not server.abandoned()
    time.sleep(0.2)
    assert not server.abandoned()


def test_a_server_in_use_does_not_exit(cache: Path) -> None:
    server = _daemon(0.2)
    shim.register_session("one", os.getpid())
    server.abandoned()
    shim.unregister_session("one")
    server.abandoned()
    server.opened()
    time.sleep(0.3)
    assert not server.abandoned()
    server.closed()
    assert not server.abandoned()
    time.sleep(0.3)
    assert server.abandoned()


def test_a_negative_grace_turns_the_exit_off(cache: Path) -> None:
    server = _daemon(-1)
    shim.register_session("one", os.getpid())
    assert not server.abandoned()
    shim.unregister_session("one")
    time.sleep(0.1)
    assert not server.abandoned()


def test_a_crashed_agent_counts_as_gone(cache: Path) -> None:
    server = _daemon(0.2)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        shim.register_session("crash", proc.pid)
        assert not server.abandoned()
    finally:
        proc.kill()
        proc.wait()
    assert not server.abandoned()
    time.sleep(0.3)
    assert server.abandoned()


def test_the_running_server_exits_after_its_last_session_unregisters(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    env = {"CODEBASE_KG_SESSION_GRACE": "1", "CODEBASE_KG_PORT": str(_free_port())}
    proc = _start(cache, tmp_path, **env)
    previous = os.environ.get("CODEBASE_KG_CACHE_DIR")
    os.environ["CODEBASE_KG_CACHE_DIR"] = str(cache)
    try:
        _state(cache)
        shim.register_session("live", os.getpid())
        time.sleep(daemon.ORPHAN_CHECK_INTERVAL + 2)
        assert proc.poll() is None, "exited with a session registered"
        shim.unregister_session("live")
        proc.wait(timeout=daemon.ORPHAN_CHECK_INTERVAL * 3 + 5)
        assert not (cache / f"server-{BUILD}.json").exists()
    finally:
        if previous is None:
            os.environ.pop("CODEBASE_KG_CACHE_DIR", None)
        else:
            os.environ["CODEBASE_KG_CACHE_DIR"] = previous
        _stop(proc)

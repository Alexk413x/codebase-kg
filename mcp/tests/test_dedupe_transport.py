"""The helpers the server, transports, hooks and launchers share."""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import (
    control,
    core,
    http_transport,
    resolve,
    shim,
    tcp_transport,
    worker,
)

ROOT = Path(__file__).resolve().parents[2]
LOADER = ROOT / "hooks" / "_shim_loader.py"
BUILD = "0.0.1+" + "b" * 12


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(path))
    return path


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 7.5), ("", 7.5), ("  ", 7.5), ("junk", 7.5), ("nan", 7.5), ("2.5", 2.5), (" 3 ", 3.0),
    ("0", 0.0), ("-1", -1.0), ("inf", float("inf")),
])
def test_env_float_tolerates_junk(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float) -> None:
    monkeypatch.delenv("KG_TEST_VALUE", raising=False)
    if raw is not None:
        monkeypatch.setenv("KG_TEST_VALUE", raw)
    assert shim.env_float("KG_TEST_VALUE", 7.5) == expected


def test_env_float_skips_a_junk_name_for_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_TEST_FIRST", "lots")
    monkeypatch.setenv("KG_TEST_SECOND", "4")
    assert shim.env_float(("KG_TEST_FIRST", "KG_TEST_SECOND"), 1.0) == 4.0


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 9), ("junk", 9), ("nan", 9), ("inf", 9), ("-inf", 9), ("12", 12), ("12.9", 12), ("0", 0),
])
def test_env_int_ignores_what_is_not_a_finite_number(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int,
) -> None:
    monkeypatch.delenv("KG_TEST_VALUE", raising=False)
    if raw is not None:
        monkeypatch.setenv("KG_TEST_VALUE", raw)
    assert shim.env_int("KG_TEST_VALUE", 9) == expected


def test_env_int_skips_infinity_for_the_next_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_TEST_FIRST", "inf")
    monkeypatch.setenv("KG_TEST_SECOND", "3")
    assert shim.env_int(("KG_TEST_FIRST", "KG_TEST_SECOND"), 9) == 3


@pytest.mark.parametrize("name", ["CODEBASE_KG_PORT", "CLAUDE_PLUGIN_OPTION_SERVER_PORT"])
@pytest.mark.parametrize("raw", ["inf", "nan", "junk", "70000", "-5"])
def test_http_port_falls_back_on_a_bad_setting(monkeypatch: pytest.MonkeyPatch, name: str, raw: str) -> None:
    monkeypatch.delenv("CODEBASE_KG_PORT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SERVER_PORT", raising=False)
    monkeypatch.setenv(name, raw)
    assert shim.http_port() == shim.DEFAULT_HTTP_PORT


def test_the_shim_loads_with_a_junk_timeout_in_the_environment() -> None:
    env = {**os.environ, "CODEBASE_KG_SHARED_TIMEOUT": "soon", "CODEBASE_KG_SPAWN_TIMEOUT": "nan"}
    code = f"import sys; sys.path.insert(0, {str(shim.PACKAGE_DIR)!r}); import shim; print(shim.CONNECT_BUDGET, shim.SPAWN_BUDGET)"
    done = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, env=env,
                          timeout=30, check=True)
    assert done.stdout.split() == ["10.0", "25.0"]


def test_write_atomic_replaces_a_file_and_leaves_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    shim.write_atomic(target, b"one")
    shim.write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores POSIX mode bits")
def test_write_atomic_keeps_the_file_private(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    shim.write_atomic(target, b"secret")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_atomic_retries_a_replace_that_windows_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"
    real = os.replace
    refusals = [PermissionError("open elsewhere")] * 3

    def flaky(src: Any, dst: Any) -> None:
        if refusals:
            raise refusals.pop()
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(shim, "_REPLACE_WAIT", 0.0)
    shim.write_atomic(target, b"later")
    assert target.read_bytes() == b"later"
    assert refusals == []


def test_concurrent_atomic_writes_never_leave_a_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    payloads = [bytes([65 + i]) * 4096 for i in range(6)]
    gate = threading.Barrier(len(payloads))

    def write(data: bytes) -> None:
        gate.wait(20)
        for _ in range(20):
            shim.write_atomic(target, data)

    threads = [threading.Thread(target=write, args=(d,)) for d in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert target.read_bytes() in payloads
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_registered_session_is_replaced_in_place(cache: Path) -> None:
    assert shim.register_session("sess-1", 1234)
    assert shim.register_session("sess-1", 5678)
    record = json.loads((shim.sessions_dir() / "sess-1.json").read_text(encoding="utf-8"))
    assert record["pid"] == 5678
    assert [p.name for p in shim.sessions_dir().iterdir()] == ["sess-1.json"]


@pytest.mark.parametrize("value", [None, True, False, 1.5, [], {}, "", 0, "abc", 7])
def test_valid_id_accepts_only_a_string_or_integer(value: Any) -> None:
    expected = value if isinstance(value, (str, int)) and not isinstance(value, bool) else None
    assert shim.valid_id({"id": value}) == expected


def test_valid_id_is_none_without_an_id() -> None:
    assert shim.valid_id({"method": "ping"}) is None


def test_rpc_error_builds_the_envelope() -> None:
    assert shim.rpc_error(3, -32601, "Method not found") == {
        "jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found"},
    }
    assert shim.rpc_error(None, -32022, "Unsupported", {"supported": ["x"]})["error"]["data"] == {"supported": ["x"]}


def test_every_transport_builds_errors_with_the_one_envelope() -> None:
    engine = core.Core(None)
    assert core.classic(engine, None, {"id": 4, "method": "nope"}) == shim.rpc_error(
        4, core.METHOD_NOT_FOUND, "Method not found")


def test_the_session_serves_parse_and_invalid_request_errors() -> None:
    out = io.BytesIO()
    tcp_transport.serve_lines(core.Core(None), resolve.Connection(cwd=Path.cwd()), io.BytesIO(b"{not json\n[1]\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies == [
        shim.rpc_error(None, core.PARSE_ERROR, "Parse error"),
        shim.rpc_error(None, core.INVALID_REQUEST, "Invalid request"),
    ]


@pytest.mark.parametrize("cwd", [None, "", "relative/dir", ".", 5, ["/abs"]])
def test_connection_refuses_a_cwd_that_is_not_absolute(cwd: Any) -> None:
    assert resolve.connection(cwd, "g.db") is None


def test_connection_binds_the_graph_to_the_cwd(tmp_path: Path) -> None:
    conn = resolve.connection(str(tmp_path), "knowledge/g.db")
    assert conn is not None
    assert conn.cwd == tmp_path
    assert conn.explicit == tmp_path / "knowledge" / "g.db"
    assert conn.resolved is None


def test_connection_accepts_a_path_and_no_graph(tmp_path: Path) -> None:
    conn = resolve.connection(tmp_path, None)
    assert conn is not None and conn.cwd == tmp_path and conn.explicit is None
    other = resolve.connection(str(tmp_path), "")
    assert other is not None and other.explicit is None


def test_an_absolute_graph_path_overrides_the_cwd(tmp_path: Path) -> None:
    graph = tmp_path / "elsewhere.db"
    conn = resolve.connection(str(tmp_path / "repo"), str(graph))
    assert conn is not None and conn.explicit == graph


def test_both_transports_build_the_same_connection(tmp_path: Path) -> None:
    hello = json.dumps({"token": "t", "version": "v", "cwd": str(tmp_path), "graph_path": "g.db"}).encode()
    from_tcp = tcp_transport.accept(hello, "t", "v")
    from_http = http_transport.classic_connection(
        {http_transport.CWD_HEADER: str(tmp_path), http_transport.GRAPH_HEADER: "g.db"})
    assert from_http == from_tcp


def test_http_refuses_a_relative_cwd_header() -> None:
    clients = http_transport.Clients()
    found = clients.connection({http_transport.CWD_HEADER: "rel"}, {})
    assert isinstance(found, dict) and found["isError"] is True
    assert http_transport.classic_connection({http_transport.CWD_HEADER: "rel"}) is None
    assert http_transport.classic_connection({}) is None


def test_tcp_handshake_refuses_a_relative_cwd_or_a_non_string_graph() -> None:
    def hello(**fields: Any) -> bytes:
        return json.dumps({"token": "t", "version": "v", **fields}).encode()

    with pytest.raises(ValueError, match="absolute"):
        tcp_transport.accept(hello(cwd="rel"), "t", "v")
    with pytest.raises(ValueError, match="absolute"):
        tcp_transport.accept(hello(), "t", "v")
    with pytest.raises(ValueError, match="graph_path"):
        tcp_transport.accept(hello(cwd=str(Path.cwd()), graph_path=3), "t", "v")


class _Holder:
    """A listener that records what the shutdown request carries and answers 200."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.requests: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    def close(self) -> None:
        self.sock.close()


def test_request_shutdown_posts_the_token() -> None:
    holder = _Holder()
    try:
        shim.request_shutdown(holder.port, "secret-token")
    finally:
        holder.close()
    head = holder.requests[0].decode()
    assert head.startswith("POST /shutdown HTTP/1.1")
    assert "Authorization: Bearer secret-token" in head
    assert f"Host: 127.0.0.1:{holder.port}" in head


def test_request_shutdown_raises_when_nothing_listens() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises((OSError, http.client.HTTPException)):
        shim.request_shutdown(port, "t")


def test_stop_reports_a_server_it_cannot_reach(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"token": "tok"}
    monkeypatch.setattr(shim, "verified_holder", lambda port: ({"pid": 1}, state))

    def refuse(port: int, token: str) -> None:
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(shim, "request_shutdown", refuse)
    found = control.stop(1)
    assert found["ok"] is False and "could not reach the server: down" in str(found["error"])


def test_claim_port_asks_the_holder_to_stop_and_goes_on_when_it_cannot_reach_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[int, str]] = []

    def refuse(port: int, token: str) -> None:
        asked.append((port, token))
        raise ConnectionResetError("gone")

    monkeypatch.setattr(http_transport, "bind", lambda port: (_ for _ in ()).throw(OSError("in use")))
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.OURS, {"build": "0.0.1+a", "version": "0.0.1", "built": 1.0}))
    monkeypatch.setattr(shim, "verified_holder", lambda port: ({}, {"token": "theirs"}))
    monkeypatch.setattr(shim, "request_shutdown", refuse)
    monkeypatch.setattr(http_transport, "HANDOVER_TIMEOUT", 0.2)
    mine = {"build": "0.0.2+b", "version": "0.0.2", "built": 2.0}
    assert http_transport.claim_port(4321, mine) is None
    assert asked == [(4321, "theirs")]


def test_base_python_skips_a_venv_launcher() -> None:
    assert shim.base_python() == (getattr(sys, "_base_executable", None) or sys.executable)
    assert shim.server_command("--serve")[0] == shim.base_python()


def test_the_worker_command_uses_the_base_interpreter() -> None:
    from codebase_kg import pool

    assert pool.worker_command()[0] == shim.base_python()


def test_no_window_is_a_creation_flag_only_on_windows() -> None:
    if sys.platform == "win32":
        assert shim.NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        assert shim.NO_WINDOW == 0


def test_the_spawn_lock_is_held_only_inside_the_block(cache: Path) -> None:
    with shim.spawn_lock(BUILD) as first:
        assert first is True
        assert shim.lock_path(BUILD).exists()
        with shim.spawn_lock(BUILD) as second:
            assert second is False
        assert shim.lock_path(BUILD).exists()
    assert not shim.lock_path(BUILD).exists()


def test_the_spawn_lock_is_released_when_the_block_raises(cache: Path) -> None:
    with pytest.raises(RuntimeError), shim.spawn_lock(BUILD):
        raise RuntimeError("boom")
    assert not shim.lock_path(BUILD).exists()


def test_the_lock_functions_are_public() -> None:
    assert not hasattr(shim, "_take_lock") and not hasattr(shim, "_release_lock")
    assert callable(shim.take_lock) and callable(shim.release_lock)


def test_the_write_tools_are_the_keys_of_the_dispatch_table() -> None:
    assert frozenset(worker._WRITES) == worker.WRITE_TOOLS
    assert worker.WRITE_TOOLS == {
        "kg_upsert_node", "kg_delete_node", "kg_add_link", "kg_remove_link",
        "kg_add_reference", "kg_remove_reference",
    }
    assert not hasattr(worker, "_writes")


def test_the_loader_returns_the_shim_without_the_package() -> None:
    spec = importlib.util.spec_from_file_location("loader_under_test", LOADER)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    module = loader.load_shim()
    assert Path(module.__file__) == shim.PACKAGE_DIR / "shim.py"
    assert module is not shim
    assert module.http_port() == shim.http_port()

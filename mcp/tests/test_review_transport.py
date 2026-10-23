"""Regressions for transport, shim, pool and registry defects found in review."""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import (
    codex_config,
    control,
    core,
    http_transport,
    resolve,
    shim,
    tcp_transport,
)
from codebase_kg.pool import Pool
from codebase_kg.shim import Relay, _Link, _messages

TIMEOUT = 20.0
BUILD = "0.0.1+" + "a" * 12


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(path))
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A port that accepts connections in the kernel backlog and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


# --- 31: the HTTP token file ---------------------------------------------------
def test_an_empty_token_file_is_replaced(cache: Path) -> None:
    (cache / "http-token").write_text("", encoding="utf-8")
    token = shim.http_token(create=True)
    assert token
    assert (cache / "http-token").read_text(encoding="utf-8") == token
    assert shim.http_token(create=True) == token


def test_concurrent_creators_share_one_token(cache: Path) -> None:
    gate = threading.Barrier(8)
    seen: list[str | None] = []

    def create() -> None:
        gate.wait(TIMEOUT)
        seen.append(shim.http_token(create=True))

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert len(seen) == 8 and len(set(seen)) == 1 and seen[0]


# --- 32: a port that accepts and never answers ---------------------------------
def test_a_port_that_accepts_and_stays_silent_is_unresponsive(silent_port: int) -> None:
    assert shim.health(silent_port, timeout=0.3) == (shim.UNRESPONSIVE, {})


def test_a_port_that_hangs_up_is_another_program() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)

    def hang_up() -> None:
        conn, _ = sock.accept()
        conn.close()

    thread = threading.Thread(target=hang_up, daemon=True)
    thread.start()
    try:
        assert shim.health(sock.getsockname()[1], timeout=2.0)[0] == shim.OTHER
    finally:
        thread.join(TIMEOUT)
        sock.close()


def test_status_does_not_call_an_unresponsive_port_another_program(silent_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.UNRESPONSIVE, {}))
    found = control.status(silent_port)
    assert found["ok"] is False
    assert "another program" not in str(found["error"])


def test_claim_port_leaves_an_unresponsive_port_to_its_holder(
    silent_port: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(http_transport, "bind", lambda port: (_ for _ in ()).throw(OSError("in use")))
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.UNRESPONSIVE, {}))
    assert http_transport.claim_port(silent_port, shim.build_info()) is None
    assert "another program" not in caplog.text


# --- 33: the spawn lock --------------------------------------------------------
def test_a_lock_held_by_a_dead_process_is_taken(cache: Path) -> None:
    shim.lock_path(BUILD).write_text(str(_dead_pid()), encoding="utf-8")
    assert shim._take_lock(BUILD)
    assert shim.lock_path(BUILD).read_text(encoding="utf-8") == str(os.getpid())


def test_a_lock_held_by_a_live_process_is_kept(cache: Path) -> None:
    shim.lock_path(BUILD).write_text(str(os.getpid()), encoding="utf-8")
    assert not shim._take_lock(BUILD)


def test_a_lock_with_no_pid_yet_is_kept(cache: Path) -> None:
    shim.lock_path(BUILD).write_text("", encoding="utf-8")
    assert not shim._take_lock(BUILD)


# --- 34: an unusable roots answer ----------------------------------------------
@pytest.mark.parametrize("roots", [
    [],
    [{"uri": "file://fileserver/share/project"}],
    [{"uri": "https://example.com/project"}],
    [{"name": "no uri"}],
])
def test_an_unusable_roots_answer_ends_the_exchange(roots: list[dict[str, Any]]) -> None:
    answer = {http_transport.ROOTS_REQUEST: {"roots": roots}}
    found = http_transport.Clients().connection(
        {http_transport.CLIENT_HEADER: "client-1"}, {"inputResponses": answer})
    assert isinstance(found, dict)
    assert found.get("resultType") != "input_required"
    assert found["isError"] is True


def test_no_roots_answer_still_asks_for_roots() -> None:
    found = http_transport.Clients().connection({http_transport.CLIENT_HEADER: "client-1"}, {})
    assert isinstance(found, dict) and found["resultType"] == "input_required"


# --- 35: a worker killed while idle --------------------------------------------
FAKE_WORKER = """
import json, os, sys
for line in sys.stdin:
    req = json.loads(line)
    sys.stdout.write(json.dumps({"id": req["id"], "ok": True, "result": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
"""


def test_an_idle_worker_killed_from_outside_is_not_reused(tmp_path: Path) -> None:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    pool = Pool(2, idle=60.0, timeout=TIMEOUT, command=[sys.executable, "-I", str(script)])
    try:
        first = pool.call("kg_search", {}, str(tmp_path / "g.db"))
        victim = pool._idle[0]
        victim.proc.kill()
        victim.proc.wait(TIMEOUT)
        second = pool.call("kg_search", {}, str(tmp_path / "g.db"))
        assert second["pid"] != first["pid"]
        assert victim not in pool._idle
    finally:
        pool.close()


# --- 36: lines queued while the relay reconnects -------------------------------
class _FlakyLink(_Link):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        if len(self.sent) + 1 == self.fail_on:
            raise OSError("broken pipe")
        self.sent.append(data)

    def close_write(self) -> None:
        pass

    def close(self) -> None:
        pass


def _request(request_id: int) -> tuple[bytes, list[dict[str, Any]]]:
    line = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {}}).encode() + b"\n"
    return line, _messages(line)


def test_every_queued_request_is_tracked_when_a_replay_send_fails() -> None:
    relay = Relay({}, [])
    relay._queue = [_request(1), _request(2), _request(3)]
    link = _FlakyLink(fail_on=2)
    assert relay._resume(link)
    assert set(relay._inflight) == {1, 2, 3}
    assert relay._link is link


def test_a_closed_private_pipe_during_replay_is_not_fatal() -> None:
    class Closed(_FlakyLink):
        def send(self, data: bytes) -> None:
            raise ValueError("write to closed file")

    relay = Relay({}, [])
    relay._queue = [_request(1), _request(2)]
    assert relay._resume(Closed(fail_on=0))
    assert set(relay._inflight) == {1, 2}


def test_requests_that_arrive_during_the_replay_are_sent_in_order() -> None:
    relay = Relay({}, [])
    relay._queue = [_request(1)]

    writers: list[threading.Thread] = []

    class Late(_FlakyLink):
        def send(self, data: bytes) -> None:
            super().send(data)
            if len(self.sent) == 1:
                writer = threading.Thread(target=relay._from_client, args=(_request(2)[0],), daemon=True)
                writer.start()
                writers.append(writer)

    link = Late(fail_on=0)
    assert relay._resume(link)
    for writer in writers:
        writer.join(TIMEOUT)
    assert [json.loads(line)["id"] for line in link.sent] == [1, 2]
    assert relay._queue == []


# --- 37: a tool call that raises -----------------------------------------------
def _call_request(request_id: int = 5) -> bytes:
    msg = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
           "params": {"name": "kg_search", "arguments": {"query": "x"}}}
    return json.dumps(msg).encode() + b"\n"


def test_a_tool_call_that_raises_still_gets_a_reply(tmp_path: Path) -> None:
    conn = resolve.Connection(cwd=tmp_path, explicit=Path(tmp_path, "a\0b"))
    out = io.BytesIO()
    tcp_transport.serve_lines(core.Core(None), conn, io.BytesIO(_call_request()), out)
    reply = json.loads(out.getvalue())
    assert reply["id"] == 5
    assert "result" in reply or "error" in reply


def test_a_non_call_message_that_raises_does_not_end_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = core.classic

    def classic(engine: Any, conn: Any, msg: dict[str, Any]) -> dict[str, Any] | None:
        if msg.get("method") == "ping":
            raise RuntimeError("boom")
        return real(engine, conn, msg)

    monkeypatch.setattr(core, "classic", classic)
    lines = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    out = io.BytesIO()
    tcp_transport.serve_lines(core.Core(None), resolve.Connection(cwd=tmp_path), io.BytesIO(lines), out)
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert "error" in replies[1]
    assert "tools" in replies[2]["result"]


@pytest.fixture
def front() -> Iterator[int]:
    sock = http_transport.bind(0)
    server = http_transport.HttpServer(
        sock, core.Core(None), "tok", "shutdown-tok", {"service": shim.OURS},
        on_request=lambda: None, on_shutdown=lambda: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(TIMEOUT)


def _post(port: int, headers: dict[str, str], body: bytes) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request("POST", "/mcp", body=body, headers={
            "Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
            "Accept": "application/json", **headers})
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw


def test_an_http_tool_call_that_raises_gets_an_error_result(front: int, tmp_path: Path) -> None:
    version = http_transport.MODERN_PROTOCOL
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "kg_search", "arguments": {"query": "x"},
        "_meta": {http_transport.VERSION_KEY: version, http_transport.CAPABILITIES_KEY: {}},
    }}).encode()
    status, reply = _post(front, {
        "Authorization": "Bearer tok", "mcp-protocol-version": version, "mcp-method": "tools/call",
        "mcp-name": "kg_search", "X-Codebase-KG-Cwd": str(tmp_path), "X-Codebase-KG-Graph": "a%00b",
    }, body)
    assert status == 200
    assert reply["result"]["isError"] is True


def test_a_classic_http_tool_call_that_raises_gets_a_reply(front: int, tmp_path: Path) -> None:
    status, reply = _post(front, {
        "Authorization": "Bearer tok", "mcp-protocol-version": core.CLASSIC_VERSIONS[-1],
        "X-Codebase-KG-Cwd": str(tmp_path), "X-Codebase-KG-Graph": "a%00b",
    }, _call_request())
    assert status == 200
    assert reply["id"] == 5 and ("result" in reply or "error" in reply)


# --- 38: a token that is not ASCII ---------------------------------------------
def test_a_non_ascii_bearer_token_is_refused_not_raised(front: int) -> None:
    status, _ = _post(front, {"Authorization": "Bearer t\xf6k"}, b"{}")
    assert status == 403


def test_a_non_ascii_authorization_value_is_not_authorized() -> None:
    assert not http_transport._authorized("Bearer t\xf6k", "tok")
    assert not http_transport._authorized("Bearer tok", "t\xf6k")
    assert http_transport._authorized("Bearer t\xf6k", "t\xf6k")


@pytest.mark.parametrize("offered", ["t\u00f6k", "\ud800"])
def test_a_non_ascii_handshake_token_is_a_bad_token(offered: str, tmp_path: Path) -> None:
    line = json.dumps({"token": offered, "version": "v", "cwd": str(tmp_path)}).encode()
    with pytest.raises(ValueError, match="bad token"):
        tcp_transport.accept(line, "tok", "v")


# --- 40: a path with an apostrophe in the Codex config -------------------------
def test_the_stdio_entry_survives_an_apostrophe_in_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = tmp_path / "O'Brien" / "launch"
    monkeypatch.setattr(codex_config, "LAUNCH", launch)
    tomllib = pytest.importorskip("tomllib")
    table = tomllib.loads(codex_config.stdio_entry("linux"))["mcp_servers"]["codebase-kg"]
    assert table["command"] == str(launch / "kg-shim")


def test_the_stdio_entry_keeps_backslashes_literal_without_an_apostrophe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_config, "LAUNCH", Path(r"C:\Users\Al\launch"))
    tomllib = pytest.importorskip("tomllib")
    table = tomllib.loads(codex_config.stdio_entry("win32"))["mcp_servers"]["codebase-kg"]
    assert table["command"] == str(Path(r"C:\Users\Al\launch") / "kg-shim.cmd")


# --- 41: the session registry on a file another process holds ------------------
def test_register_session_retries_a_refused_replace(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace
    refusals = [3]

    def flaky(src: Any, dst: Any) -> None:
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    assert shim.register_session("s1", os.getpid())
    assert shim.live_sessions() == ["s1"]
    assert not list(shim.sessions_dir().glob("*.tmp"))


def test_unregister_session_retries_a_refused_unlink(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim.register_session("s1", os.getpid())
    real = Path.unlink
    refusals = [3]

    def flaky(self: Path, *args: Any, **kwargs: Any) -> None:
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(5, "Access is denied")
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    assert shim.unregister_session("s1")
    assert shim.live_sessions() == []


def test_unregister_session_gives_up_on_a_file_that_stays_locked(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim.register_session("s1", os.getpid())

    def locked(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "unlink", locked)
    assert not shim.unregister_session("s1")

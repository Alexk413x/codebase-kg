"""The classic line transport answers the calls in flight before it returns."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import core, resolve, tcp_transport


def test_serve_lines_answers_a_slow_call_after_input_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()

    def classic(engine: Any, conn: Any, msg: dict[str, Any]) -> dict[str, Any]:
        release.wait(5)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}

    monkeypatch.setattr(core, "classic", classic)
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}
    out = io.BytesIO()
    threading.Timer(0.2, release.set).start()
    tcp_transport.serve_lines(
        core.Core(None), resolve.Connection(cwd=tmp_path),
        io.BytesIO(json.dumps(request).encode() + b"\n"), out,
    )
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_only_an_authenticated_connection_counts_as_a_use_of_the_server() -> None:
    import socket

    events: list[str] = []
    server = tcp_transport.TcpServer(
        core.Core(None), "1.0", "secret", lambda: events.append("open"), lambda: events.append("close"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def hello(payload: dict[str, Any]) -> dict[str, Any]:
            with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
                sock.sendall(json.dumps(payload).encode() + b"\n")
                reply = sock.makefile("rb").readline()
            return json.loads(reply)

        assert hello({"token": "wrong", "version": "1.0", "cwd": str(Path.cwd())})["ok"] is False
        with socket.create_connection(("127.0.0.1", server.port), timeout=5):
            pass  # connects and says nothing
        assert hello({"token": "wrong", "version": "1.0"})["ok"] is False
        assert events == []
        assert hello({"token": "secret", "version": "1.0", "cwd": str(Path.cwd())})["ok"] is True
        deadline = threading.Event()
        for _ in range(50):
            if events == ["open", "close"]:
                break
            deadline.wait(0.1)
        assert events == ["open", "close"]
    finally:
        server.shutdown()
        server.server_close()

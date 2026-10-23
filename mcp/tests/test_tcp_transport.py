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

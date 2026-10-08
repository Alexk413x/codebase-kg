"""Classic MCP over newline-delimited JSON: the shims' TCP listener and the private stdio server. Stdlib only.

A shim connection opens with one JSON handshake line — the token, the build,
and the session's cwd and explicit graph path — and the server answers with one
line before any MCP traffic flows. It refuses a handshake with a wrong token or
from any other build. Then the session speaks classic MCP (`initialize`,
`tools/list`, `tools/call`), answered by `core.classic`. Each tool call runs on
its own thread, so one session's slow call does not hold up its next one.

A private server (the shim's fallback, or `CODEBASE_KG_SHARED=0`) speaks the
same over its own stdin and stdout, for the one session that started it.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import socketserver
import threading
from collections.abc import Callable
from io import BufferedIOBase
from pathlib import Path
from typing import IO, Any, cast

from . import core, resolve

logger = logging.getLogger("codebase_kg.tcp")

HANDSHAKE_TIMEOUT = 5.0
MAX_HANDSHAKE = 64 * 1024
MAX_MESSAGE = 64 * 1024 * 1024


def serve_lines(
    engine: core.Core, conn: resolve.Connection,
    lines: IO[bytes] | BufferedIOBase, out: IO[bytes] | BufferedIOBase,
) -> None:
    """Answer one classic MCP session until its input ends and its calls in flight are answered."""
    lock = threading.Lock()
    calls: list[threading.Thread] = []

    def write(obj: dict[str, Any]) -> None:
        data = core.dumps(obj).encode("utf-8") + b"\n"
        with lock:
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                pass

    def call(msg: dict[str, Any]) -> None:
        reply = core.classic(engine, conn, msg)
        if reply is not None:
            write(reply)

    while True:
        raw = lines.readline(MAX_MESSAGE)
        if not raw:
            return
        if not raw.endswith(b"\n") and len(raw) >= MAX_MESSAGE:
            logger.warning("a message over %d bytes; closing the session", MAX_MESSAGE)
            return
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            write({"jsonrpc": "2.0", "id": None, "error": {"code": core.PARSE_ERROR, "message": "Parse error"}})
            continue
        if not isinstance(msg, dict):
            write({"jsonrpc": "2.0", "id": None,
                   "error": {"code": core.INVALID_REQUEST, "message": "Invalid request"}})
            continue
        if msg.get("method") == "tools/call":
            calls = [t for t in calls if t.is_alive()]
            thread = threading.Thread(target=call, args=(msg,), daemon=True)
            thread.start()
            calls.append(thread)
        else:
            call(msg)
    for thread in calls:
        thread.join()


def accept(line: bytes, token: str, version: str) -> resolve.Connection:
    """Validate a handshake line, or raise ValueError naming what is wrong."""
    try:
        hello = json.loads(line)
    except ValueError as exc:
        raise ValueError(f"malformed handshake: {exc}") from None
    if not isinstance(hello, dict):
        raise ValueError("malformed handshake")  # noqa: TRY004 - callers catch ValueError
    offered = hello.get("token")
    if not isinstance(offered, str) or not hmac.compare_digest(offered, token):
        raise ValueError("bad token")
    if hello.get("version") != version:
        raise ValueError(f"version mismatch: server is {version}, client is {hello.get('version')}")
    cwd = hello.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("cwd must be an absolute path")
    graph_path = hello.get("graph_path")
    if graph_path is not None and not isinstance(graph_path, str):
        raise ValueError("graph_path must be a string")
    return resolve.Connection(cwd=Path(cwd), explicit=Path(cwd, graph_path) if graph_path else None)


class TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 128

    def __init__(
        self, engine: core.Core, version: str, token: str,
        on_open: Callable[[], None], on_close: Callable[[], None],
    ) -> None:
        super().__init__(("127.0.0.1", 0), TcpHandler)
        self.core = engine
        self.version = version
        self.token = token
        self.on_open = on_open
        self.on_close = on_close

    @property
    def port(self) -> int:
        return self.server_address[1]


class TcpHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        srv = cast(TcpServer, self.server)
        srv.on_open()
        try:
            self.connection.settimeout(HANDSHAKE_TIMEOUT)
            line = self.rfile.readline(MAX_HANDSHAKE)
            try:
                conn = accept(line, srv.token, srv.version)
            except ValueError as exc:
                logger.warning("rejected a connection: %s", exc)
                self._reply({"ok": False, "error": str(exc)})
                return
            self._reply({"ok": True, "version": srv.version, "pid": os.getpid()})
            self.connection.settimeout(None)
            serve_lines(srv.core, conn, self.rfile, self.wfile)
        except (OSError, ValueError):
            pass
        except Exception:
            # One broken session must not take the listener, and every other session with it, down.
            logger.exception("connection failed")
        finally:
            srv.on_close()

    def _reply(self, obj: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(obj).encode("utf-8") + b"\n")
        self.wfile.flush()

    def finish(self) -> None:
        try:
            super().finish()
        except OSError:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

"""`codebase-kg --serve`: one server process for every session on the machine.

`shim.py` starts this on first use and relays each session to it over a
loopback TCP connection. The server binds 127.0.0.1 on a port the OS picks and
publishes `{version, port, pid, token}` in a per-version state file readable
only by the user. A connection opens with one JSON handshake line — the token,
the version, and the session's cwd and explicit graph path — and the server
answers with one line before any MCP traffic flows.

Each accepted connection runs its own MCP session over the socket, with the
same tool registrations the stdio server uses, bound to that connection's
`server.Connection`. The server exits once it has had no connection for
`CODEBASE_KG_IDLE_TIMEOUT` seconds (default 600) and removes its state file.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter

from . import server, shim

logger = logging.getLogger("codebase_kg.daemon")

DEFAULT_IDLE_TIMEOUT = 600.0
HANDSHAKE_TIMEOUT = 5.0
MAX_HANDSHAKE = 64 * 1024
MAX_MESSAGE = 64 * 1024 * 1024


def idle_timeout() -> float:
    raw = os.environ.get("CODEBASE_KG_IDLE_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else DEFAULT_IDLE_TIMEOUT
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT


def _write_private(path: Path, data: bytes) -> None:
    """Replace `path` atomically with a file only this user can read.

    On Windows the mode bits are ignored; the per-user cache directory's
    inherited ACL is what keeps the token private there.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses to replace a file another process has open, and a
            # shim may be reading the previous state at this instant.
            time.sleep(0.02)
    os.replace(tmp, path)


class Daemon:
    def __init__(self, version: str, token: str, timeout: float) -> None:
        self.version = version
        self.token = token
        self.timeout = timeout
        self.active = 0
        self.last_seen = time.monotonic()

    def accept(self, line: bytes) -> server.Connection:
        """Validate a handshake line, or raise ValueError naming what is wrong."""
        try:
            hello = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"malformed handshake: {exc}") from None
        if not isinstance(hello, dict):
            raise ValueError("malformed handshake")
        token = hello.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            raise ValueError("bad token")
        if hello.get("version") != self.version:
            raise ValueError(
                f"version mismatch: server is {self.version}, client is {hello.get('version')}"
            )
        cwd = hello.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        graph_path = hello.get("graph_path")
        if graph_path is not None and not isinstance(graph_path, str):
            raise ValueError("graph_path must be a string")
        explicit = Path(cwd, graph_path) if graph_path else None
        return server.Connection(cwd=Path(cwd), explicit=explicit)

    async def handle(self, stream: SocketStream) -> None:
        self.active += 1
        try:
            async with stream:
                buffered = BufferedByteReceiveStream(stream)
                with anyio.fail_after(HANDSHAKE_TIMEOUT):
                    line = await buffered.receive_until(b"\n", MAX_HANDSHAKE)
                try:
                    conn = self.accept(line)
                except ValueError as exc:
                    logger.warning("rejected a connection: %s", exc)
                    await stream.send(_line({"ok": False, "error": str(exc)}))
                    return
                await stream.send(_line({"ok": True, "version": self.version, "pid": os.getpid()}))
                server.bind_connection(conn)
                await _run_session(buffered, stream)
        except (anyio.EndOfStream, anyio.IncompleteRead, anyio.BrokenResourceError,
                anyio.ClosedResourceError, anyio.DelimiterNotFound, TimeoutError):
            pass
        except Exception:
            # One broken session must not take the listener, and every other
            # session with it, down.
            logger.exception("connection failed")
        finally:
            self.active -= 1
            self.last_seen = time.monotonic()

    async def watch_idle(self) -> None:
        while True:
            await anyio.sleep(min(1.0, self.timeout / 4))
            if self.active == 0 and time.monotonic() - self.last_seen >= self.timeout:
                return


def _line(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\n"


async def _run_session(buffered: BufferedByteReceiveStream, stream: SocketStream) -> None:
    """One MCP session over a socket, framed the way the stdio transport frames it."""
    read_send, read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_recv = anyio.create_memory_object_stream[SessionMessage](0)

    async def reader() -> None:
        async with read_send:
            while True:
                try:
                    raw = await buffered.receive_until(b"\n", MAX_MESSAGE)
                except (anyio.EndOfStream, anyio.IncompleteRead):
                    return
                if not raw.strip():
                    continue
                try:
                    message = jsonrpc_message_adapter.validate_json(raw, by_name=False)
                except Exception as exc:
                    await read_send.send(exc)
                    continue
                await read_send.send(SessionMessage(message))

    async def writer() -> None:
        async with write_recv:
            async for item in write_recv:
                payload = item.message.model_dump_json(by_alias=True, exclude_unset=True)
                await stream.send(payload.encode("utf-8") + b"\n")

    lowlevel = server.mcp._mcp_server
    async with anyio.create_task_group() as tg:
        tg.start_soon(reader)
        tg.start_soon(writer)
        await lowlevel.run(
            read_recv,
            write_send,
            lowlevel.create_initialization_options(
                notification_options=NotificationOptions(tools_changed=True),
            ),
        )
        tg.cancel_scope.cancel()


async def _serve(daemon: Daemon) -> None:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    state = shim.state_path(daemon.version)
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(state, json.dumps({
        "version": daemon.version, "port": port, "pid": os.getpid(), "token": daemon.token,
    }).encode("utf-8"))
    logger.info("serving codebase-kg %s on 127.0.0.1:%d", daemon.version, port)
    try:
        # Entered once here, as run_stdio_async does, so each session's lowlevel
        # run() reuses it through FastMCP's ref count instead of re-running setup.
        async with server.mcp._lifespan_manager():
            async with listener, anyio.create_task_group() as tg:
                tg.start_soon(listener.serve, daemon.handle)
                await daemon.watch_idle()
                tg.cancel_scope.cancel()
    finally:
        _remove_if_ours(state, daemon.token)
        logger.info("shutting down")


def _remove_if_ours(state: Path, token: str) -> None:
    try:
        if json.loads(state.read_text(encoding="utf-8")).get("token") == token:
            state.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def serve() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s",
    )
    server.enter_serve_mode()
    daemon = Daemon(shim.package_version(), secrets.token_hex(32), idle_timeout())
    anyio.run(_serve, daemon)

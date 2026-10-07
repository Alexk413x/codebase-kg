"""`codebase-kg --serve`: one server process for every session on the machine.

`shim.py` starts this on first use and relays each session to it over a
loopback TCP connection. The server binds 127.0.0.1 on a port the OS picks and
publishes `{version, port, pid, token, http_port}` in a per-build state file
readable only by the user, where `version` is `shim.server_build()`: the package
version plus a digest of the source files. A connection opens with one JSON
handshake line — the token, the build, and the session's cwd and explicit graph
path — and the server answers with one line before any MCP traffic flows. It
refuses a handshake from any other build.

Each accepted connection runs its own MCP session over the socket, with the
same tool registrations the stdio server uses, bound to that connection's
`server.Connection`.

The same process serves Streamable HTTP at `http://127.0.0.1:<port>/mcp` for
Claude Code (see `http_transport.py`), on the port `shim.http_port()` names, and
records that port in the state file as `http_port`, or null when it holds none.
When the port answers as an older codebase-kg build that its state file
verifies, this server asks it to stop and takes the port. When it answers as a
newer build, or as anything it cannot verify, this server serves the shims only.

Claude Code sends each call to whatever holds the port, but nothing restarts a
server that stopped mid-session. So a server that holds the HTTP port exits only
after `CODEBASE_KG_HTTP_IDLE_TIMEOUT` seconds (default 8 hours) with no HTTP
request and no shim connection. One without it exits after
`CODEBASE_KG_IDLE_TIMEOUT` seconds (default 600) with no shim connection.
`POST /shutdown` with the server's own state-file token stops it at once.
Either way it removes its state file.

Read tool calls from every session, HTTP and shim alike, run on the elastic
worker pool in `pool.py`, sized by `pool.max_workers()` as read at start. A
running server keeps its size until it exits, so a changed `max_workers`
setting takes effect at the next server start. With 0 the calls run here.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter

from . import http_transport, server, shim
from .pool import Pool, call_timeout, max_workers

logger = logging.getLogger("codebase_kg.daemon")

DEFAULT_IDLE_TIMEOUT = 600.0
DEFAULT_HTTP_IDLE_TIMEOUT = 8 * 3600.0
HANDSHAKE_TIMEOUT = 5.0
HTTP_STOP_TIMEOUT = 5.0
ORPHAN_CHECK_INTERVAL = 5.0
MAX_HANDSHAKE = 64 * 1024
MAX_MESSAGE = 64 * 1024 * 1024


def idle_timeout(name: str = "CODEBASE_KG_IDLE_TIMEOUT", default: float = DEFAULT_IDLE_TIMEOUT) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


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
    def __init__(
        self, version: str, token: str, timeout: float,
        http_port: int | None = None, http_timeout: float = DEFAULT_HTTP_IDLE_TIMEOUT,
    ) -> None:
        self.version = version
        self.token = token
        self.timeout = timeout
        self.http_port = http_port
        self.http_timeout = http_timeout
        self.active = 0
        self.last_seen = time.monotonic()
        self.stopping = False
        self.state: Path | None = None

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def stop(self) -> None:
        self.stopping = True

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

    def orphaned(self) -> bool:
        """True once this server's state file is gone or names another server.

        Nothing can verify an orphan, so it would hold the HTTP port with every
        session refused until its idle limit.
        """
        if self.state is None:
            return False
        try:
            return json.loads(self.state.read_text(encoding="utf-8")).get("token") != self.token
        except FileNotFoundError:
            return True
        except (OSError, ValueError, AttributeError):
            return False

    async def watch_idle(self) -> None:
        limit = self.http_timeout if self.http_port is not None else self.timeout
        checked = time.monotonic()
        while not self.stopping:
            await anyio.sleep(min(0.25, limit / 4))
            if self.active == 0 and time.monotonic() - self.last_seen >= limit:
                return
            if time.monotonic() - checked >= ORPHAN_CHECK_INTERVAL:
                checked = time.monotonic()
                if self.orphaned():
                    logger.warning("state file %s is gone or not ours; exiting", self.state)
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


async def _serve_http(http: uvicorn.Server, sock: socket.socket, done: anyio.Event) -> None:
    try:
        await http.serve(sockets=[sock])
    finally:
        done.set()


def _http_server(daemon: Daemon, sock: socket.socket, info: dict[str, Any]) -> uvicorn.Server:
    app = http_transport.build_app(
        sock.getsockname()[1], shim.http_token(create=True) or "", daemon.token,
        http_transport.health_reply(info, os.getpid()),
        on_request=daemon.touch, on_shutdown=daemon.stop,
    )
    return uvicorn.Server(uvicorn.Config(
        app, lifespan="on", log_level="warning", access_log=False, timeout_graceful_shutdown=2,
    ))


async def _serve(daemon: Daemon, http_sock: socket.socket | None, info: dict[str, Any]) -> None:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    state = shim.state_path(daemon.version)
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    daemon.state = state
    _write_private(state, json.dumps({
        "version": daemon.version, "port": port, "pid": os.getpid(), "token": daemon.token,
        "http_port": daemon.http_port,
    }).encode("utf-8"))
    logger.info("serving codebase-kg %s on 127.0.0.1:%d", daemon.version, port)
    http = _http_server(daemon, http_sock, info) if http_sock is not None else None
    done = anyio.Event()
    try:
        # Entered once here, as run_stdio_async does, so each session's lowlevel
        # run() reuses it through FastMCP's ref count instead of re-running setup.
        async with server.mcp._lifespan_manager():
            async with listener, anyio.create_task_group() as tg:
                tg.start_soon(listener.serve, daemon.handle)
                if http is not None and http_sock is not None:
                    logger.info("HTTP on http://127.0.0.1:%d/mcp", daemon.http_port)
                    tg.start_soon(_serve_http, http, http_sock, done)
                await daemon.watch_idle()
                if http is not None:
                    http.should_exit = True
                    with anyio.move_on_after(HTTP_STOP_TIMEOUT):
                        await done.wait()
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
    info = shim.build_info()
    port = shim.http_port()
    http_sock = http_transport.claim_port(port, info)
    if http_sock is None:
        holder = shim.verified_holder(port)
        if holder is not None and holder[0].get("build") == info["build"]:
            # Its state file is the one shims read; a second server of this build would replace it.
            logger.info("codebase-kg %s already serves port %d; exiting", info["build"], port)
            return
    daemon = Daemon(
        info["build"], secrets.token_hex(32), idle_timeout(),
        http_port=http_sock.getsockname()[1] if http_sock is not None else None,
        http_timeout=idle_timeout("CODEBASE_KG_HTTP_IDLE_TIMEOUT", DEFAULT_HTTP_IDLE_TIMEOUT),
    )
    limit = max_workers()
    pool = Pool(limit, timeout=call_timeout()) if limit > 0 else None
    server.use_pool(pool)
    logger.info("read tools run on up to %d worker processes", limit)
    info = {**info, "max_workers": limit}
    try:
        anyio.run(_serve, daemon, http_sock, info)
    finally:
        server.use_pool(None)
        if pool is not None:
            pool.close()

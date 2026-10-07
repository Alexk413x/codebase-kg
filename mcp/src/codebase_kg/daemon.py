"""The codebase-kg server: `--serve` runs one process for every session on the machine. Stdlib only.

`shim.spawn_server` and the SessionStart hook start it on the base Python with
`-I -S`, from the plugin's `src`, with no venv and no third-party package. It
serves classic MCP to the shims over loopback TCP (`tcp_transport.py`) and MCP
2026-07-28 to Claude Code over HTTP (`http_transport.py`), both from one
`core.Core`, which runs tool calls on the worker pool in `pool.py`.

The TCP listener binds 127.0.0.1 on a port the OS picks. The server publishes
`{version, port, pid, token, http_port}` in a per-build state file readable
only by the user, where `version` is `shim.server_build()`: the package version
plus a digest of the source files. `http_port` is the port `shim.http_port()`
names, or null when this server holds none. When that port answers as an older
codebase-kg build that its state file verifies, this server asks it to stop and
takes the port. When it answers as a newer build, or as anything it cannot
verify, this server serves the shims only; when it answers as this same build,
this server exits, because its state file is the one the shims read.

Claude Code sends each call to whatever holds the port, but nothing restarts a
server that stopped mid-session. So a server that holds the HTTP port exits only
after `CODEBASE_KG_HTTP_IDLE_TIMEOUT` seconds (default 8 hours) with no HTTP
request and no shim connection. One without it exits after
`CODEBASE_KG_IDLE_TIMEOUT` seconds (default 600) with no shim connection. Both
exit within `ORPHAN_CHECK_INTERVAL` seconds once their state file is gone or
names another server, and at once on `POST /shutdown` with the state-file token.
Each removes its state file if it is still its own.

The pool size is `pool.max_workers()`, read once at start: a changed
`max_workers` setting takes effect when the server next starts. With 0 the calls
run in this process.

Without `--serve`, the process is a private server: classic MCP on its own
stdio for the one session that started it, with the graph from its first
argument, `$CODEBASE_KG_PATH` or its cwd, and no pool.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import core, http_transport, resolve, shim, tcp_transport
from .pool import Pool, call_timeout, max_workers

logger = logging.getLogger("codebase_kg.daemon")

DEFAULT_IDLE_TIMEOUT = 600.0
DEFAULT_HTTP_IDLE_TIMEOUT = 8 * 3600.0
ORPHAN_CHECK_INTERVAL = 5.0
TICK = 0.25


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
    """When to exit: idle limits, an orphaned state file, or a shutdown request."""

    def __init__(
        self, version: str, token: str, timeout: float,
        http_port: int | None = None, http_timeout: float = DEFAULT_HTTP_IDLE_TIMEOUT,
    ) -> None:
        self.version = version
        self.token = token
        self.timeout = timeout
        self.http_port = http_port
        self.http_timeout = http_timeout
        self.state: Path | None = None
        self._lock = threading.Lock()
        self._active = 0
        self._last_seen = time.monotonic()
        self._stopping = threading.Event()

    def touch(self) -> None:
        with self._lock:
            self._last_seen = time.monotonic()

    def opened(self) -> None:
        with self._lock:
            self._active += 1
            self._last_seen = time.monotonic()

    def closed(self) -> None:
        with self._lock:
            self._active -= 1
            self._last_seen = time.monotonic()

    def stop(self) -> None:
        self._stopping.set()

    def idle(self) -> bool:
        limit = self.http_timeout if self.http_port is not None else self.timeout
        with self._lock:
            return self._active == 0 and time.monotonic() - self._last_seen >= limit

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

    def wait(self) -> None:
        """Return when the server should exit."""
        checked = time.monotonic()
        while not self._stopping.wait(TICK):
            if self.idle():
                return
            if time.monotonic() - checked >= ORPHAN_CHECK_INTERVAL:
                checked = time.monotonic()
                if self.orphaned():
                    logger.warning("state file %s is gone or not ours; exiting", self.state)
                    return


def _remove_if_ours(state: Path, token: str) -> None:
    try:
        if json.loads(state.read_text(encoding="utf-8")).get("token") == token:
            state.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _start(server: Any, name: str, running: list[Any]) -> None:
    threading.Thread(target=server.serve_forever, name=name, daemon=True).start()
    running.append(server)


def serve() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s",
    )
    info = shim.build_info()
    port = shim.http_port()
    http_sock = http_transport.claim_port(port, info)
    if http_sock is None:
        holder = shim.verified_holder(port)
        if holder is not None and holder[0].get("build") == info["build"]:
            logger.info("codebase-kg %s already serves port %d; exiting", info["build"], port)
            return
    daemon = Daemon(
        info["build"], secrets.token_hex(32), idle_timeout(),
        http_port=http_sock.getsockname()[1] if http_sock is not None else None,
        http_timeout=idle_timeout("CODEBASE_KG_HTTP_IDLE_TIMEOUT", DEFAULT_HTTP_IDLE_TIMEOUT),
    )
    limit = max_workers()
    pool = Pool(limit, timeout=call_timeout()) if limit > 0 else None
    engine = core.Core(pool)
    tcp = tcp_transport.TcpServer(engine, daemon.version, daemon.token, daemon.opened, daemon.closed)
    state = shim.state_path(daemon.version)
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    daemon.state = state
    http: http_transport.HttpServer | None = None
    running: list[Any] = []
    try:
        if http_sock is not None:
            http = http_transport.HttpServer(
                http_sock, engine, shim.http_token(create=True) or "", daemon.token,
                http_transport.health_reply({**info, "max_workers": limit}, os.getpid()),
                on_request=daemon.touch, on_shutdown=daemon.stop,
            )
        _write_private(state, json.dumps({
            "version": daemon.version, "port": tcp.port, "pid": os.getpid(), "token": daemon.token,
            "http_port": daemon.http_port,
        }).encode("utf-8"))
        logger.info("serving codebase-kg %s on 127.0.0.1:%d", daemon.version, tcp.port)
        logger.info("tool calls run on up to %d worker processes", limit)
        _start(tcp, "kg-tcp", running)
        if http is not None:
            logger.info("HTTP on http://127.0.0.1:%d/mcp", daemon.http_port)
            _start(http, "kg-http", running)
        daemon.wait()
    finally:
        _remove_if_ours(state, daemon.token)
        for server in running:
            server.shutdown()
        for server in (http, tcp):
            if server is not None:
                server.server_close()
        if http is None and http_sock is not None:
            http_sock.close()
        if pool is not None:
            pool.close()
        logger.info("shutting down")


def private(argv: list[str]) -> None:
    tcp_transport.serve_lines(core.Core(None), resolve.from_process(argv), sys.stdin.buffer, sys.stdout.buffer)


def main(argv: list[str] | None = None) -> None:
    # Resolve nothing here: the server must start cleanly in a repo that has no
    # graph yet (e.g. before /codebase-kg:build). A missing graph surfaces as an
    # actionable error on first use, not as a server that refuses to start.
    args = sys.argv if argv is None else argv
    if args[1:2] == ["--serve"]:
        serve()
    else:
        private(args)


if __name__ == "__main__":
    main()

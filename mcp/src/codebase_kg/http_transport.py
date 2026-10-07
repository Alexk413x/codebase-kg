"""Streamable HTTP for the shared server, beside the TCP listener the shims use.

Claude Code reaches the server at `http://127.0.0.1:<port>/mcp`. `Front` sits in
front of fastmcp's app and refuses, before any MCP code runs, a request whose
`Host` is not this server's loopback address or whose `Origin` is set to
anything else, which blocks DNS rebinding from a browser. Every route but
`GET /health` needs the per-user bearer token from `shim.http_token`.

Claude Code speaks MCP 2026-07-28 over HTTP: no sessions, no `Mcp-Session-Id`,
and no server-to-client request channel. Its `headersHelper`
(`mcp/launch/kg_headers.py`) runs in the plugin folder and cannot see the
session's cwd, so it sends `X-Codebase-KG-Client`, a random id per connect, and
`X-Codebase-KG-Graph` when `CODEBASE_KG_PATH` is set. `SessionBinding` answers
the first tool call from an unseen client with an `InputRequiredResult` asking
for roots, binds the first `file:` root as that client's cwd, and caches the
`server.Connection`, so later calls cost no extra round trip. A client that
sends `X-Codebase-KG-Cwd` skips the roots request. Header values are
percent-encoded UTF-8.
"""

from __future__ import annotations

import hmac
import http.client
import logging
import socket
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import server, shim

logger = logging.getLogger("codebase_kg.http")

MODERN_PROTOCOL = "2026-07-28"
ROOTS_REQUEST = "codebase-kg-roots"
CLIENT_HEADER = "x-codebase-kg-client"
CWD_HEADER = "x-codebase-kg-cwd"
GRAPH_HEADER = "x-codebase-kg-graph"
MAX_CLIENTS = 1024
HANDOVER_TIMEOUT = 5.0
NO_CWD = (
    "codebase-kg: this HTTP client sent no working directory. Send the "
    "X-Codebase-KG-Cwd header, or answer the server's roots request."
)


class Front:
    """Host and Origin checks, the bearer token, `/health` and `/shutdown`, then MCP.

    `/mcp` takes the per-user HTTP token. `/shutdown` takes this server's own
    state-file token instead, which only a process of this user can read.
    """

    def __init__(
        self, inner: ASGIApp, port: int, token: str, shutdown_token: str, health: dict[str, Any],
        on_request: Callable[[], None], on_shutdown: Callable[[], None],
    ) -> None:
        self.inner = inner
        self.token = token
        self.shutdown_token = shutdown_token
        self.health = health
        self.on_request = on_request
        self.on_shutdown = on_shutdown
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.hosts:
            await _refuse(403, "Host is not this server", scope, receive, send)
            return
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.origins:
            await _refuse(403, "Origin not allowed", scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        if path == "/health" and method == "GET":
            await JSONResponse(self.health)(scope, receive, send)
            return
        shutdown = path == "/shutdown"
        expected = self.shutdown_token if shutdown else self.token
        if not _authorized(headers.get("authorization", ""), expected):
            # 403, not 401: a 401 sends Claude Code into its OAuth flow, which this server has none of.
            await _refuse(403, "missing or wrong bearer token", scope, receive, send)
            return
        if shutdown:
            if method != "POST":
                await _refuse(405, "use POST", scope, receive, send)
                return
            await JSONResponse({"ok": True})(scope, receive, send)
            self.on_shutdown()
            return
        self.on_request()
        await self.inner(scope, receive, send)


def _authorized(value: str, expected: str) -> bool:
    scheme, _, token = value.partition(" ")
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), expected)


async def _refuse(status: int, error: str, scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"error": error}, status_code=status)(scope, receive, send)


def _header(headers: Headers, name: str) -> str | None:
    value = headers.get(name, "").strip()
    return unquote(value) if value else None


def _root_path(responses: object) -> Path | None:
    """The first `file:` root in a client's answer to our roots request."""
    if not isinstance(responses, dict):
        return None
    answer = responses.get(ROOTS_REQUEST)
    roots = answer.get("roots") if isinstance(answer, dict) else None
    for root in roots if isinstance(roots, list) else []:
        uri = root.get("uri") if isinstance(root, dict) else None
        if not isinstance(uri, str):
            continue
        parts = urlsplit(uri)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            continue
        path = Path(url2pathname(parts.path))
        if path.is_absolute():
            return path
    return None


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


class SessionBinding:
    """Bind each HTTP tool call to its client's `server.Connection`, as the shim handshake does."""

    def __init__(self, limit: int = MAX_CLIENTS) -> None:
        self.limit = limit
        self.clients: OrderedDict[tuple[str, str | None], server.Connection] = OrderedDict()

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        request = ctx.request
        if ctx.method != "tools/call" or not isinstance(request, Request):
            return await call_next(ctx)
        found = self.connection(ctx, request.headers)
        if not isinstance(found, server.Connection):
            return found
        token = server.bind_connection(found)
        try:
            return await call_next(ctx)
        finally:
            server.unbind_connection(token)

    def connection(
        self, ctx: ServerRequestContext[Any, Any], headers: Headers,
    ) -> server.Connection | types.CallToolResult | types.InputRequiredResult:
        graph = _header(headers, GRAPH_HEADER)
        cwd = _header(headers, CWD_HEADER)
        if cwd is not None:
            if not Path(cwd).is_absolute():
                return _error("codebase-kg: X-Codebase-KG-Cwd must be an absolute path.")
            return self._new(Path(cwd), graph)
        client = _header(headers, CLIENT_HEADER)
        key = (client, graph) if client else None
        if key is not None and key in self.clients:
            self.clients.move_to_end(key)
            return self.clients[key]
        root = _root_path((ctx.params or {}).get("inputResponses"))
        if root is None:
            if ctx.protocol_version >= MODERN_PROTOCOL:
                return types.InputRequiredResult(input_requests={ROOTS_REQUEST: types.ListRootsRequest()})
            return _error(NO_CWD)
        conn = self._new(root, graph)
        if key is not None:
            self.clients[key] = conn
            while len(self.clients) > self.limit:
                self.clients.popitem(last=False)
        return conn

    @staticmethod
    def _new(cwd: Path, graph: str | None) -> server.Connection:
        return server.Connection(cwd=cwd, explicit=Path(cwd, graph) if graph else None)


def build_app(
    port: int, token: str, shutdown_token: str, health: dict[str, Any],
    on_request: Callable[[], None], on_shutdown: Callable[[], None],
) -> Front:
    lowlevel = server.mcp._mcp_server
    if not any(isinstance(mw, SessionBinding) for mw in lowlevel.middleware):
        # Outermost, so the connection is bound before fastmcp's own middleware runs the tool.
        lowlevel.middleware.insert(0, SessionBinding())  # pyright: ignore[reportArgumentType]
    inner = server.mcp.http_app(path="/mcp", host_origin_protection=False)
    return Front(inner, port, token, shutdown_token, health, on_request, on_shutdown)


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # pyright: ignore[reportAttributeAccessIssue]
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


def _ask_to_stop(port: int, token: str) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        conn.request("POST", "/shutdown", headers={
            "Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {token}", "Content-Length": "0",
        })
        conn.getresponse().read()
    except (OSError, http.client.HTTPException):
        pass
    finally:
        conn.close()


def claim_port(port: int, mine: dict[str, Any]) -> socket.socket | None:
    """Bind the HTTP port, taking it from an older codebase-kg build. None leaves it to its holder.

    The stop request goes only to a holder `shim.verified_holder` confirms, with
    that holder's own state-file token, so an unverified peer never gets a credential.
    """
    try:
        return bind(port)
    except OSError:
        pass
    kind, theirs = shim.health(port)
    if kind == shim.FREE:
        logger.warning("cannot bind 127.0.0.1:%d; serving the shims only", port)
        return None
    if kind == shim.OTHER:
        logger.warning(
            "port %d belongs to another program; set the codebase-kg server_port setting "
            "to a free port. Serving the shims only.", port,
        )
        return None
    if not shim.outranks(mine, theirs):
        logger.info("codebase-kg %s holds port %d; serving the shims only", theirs.get("build"), port)
        return None
    holder = shim.verified_holder(port)
    if holder is None:
        logger.warning(
            "port %d answers as codebase-kg %s but matches no live state file of this user; "
            "serving the shims only", port, theirs.get("build"),
        )
        return None
    logger.info("asking codebase-kg %s to hand over port %d", theirs.get("build"), port)
    _ask_to_stop(port, holder[1]["token"])
    deadline = time.monotonic() + HANDOVER_TIMEOUT
    while time.monotonic() < deadline:
        try:
            return bind(port)
        except OSError:
            time.sleep(0.1)
    logger.warning("codebase-kg %s did not hand over port %d", theirs.get("build"), port)
    return None


def health_reply(info: dict[str, Any], pid: int) -> dict[str, Any]:
    return {"service": shim.OURS, "build": info["build"], "version": info["version"],
            "built": info["built"], "pid": pid, "max_workers": info.get("max_workers")}

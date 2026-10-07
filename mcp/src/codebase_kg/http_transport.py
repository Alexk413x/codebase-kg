"""MCP over HTTP for Claude Code, on the shared server. Stdlib only.

Claude Code reaches the server at `http://127.0.0.1:<port>/mcp` and speaks MCP
2026-07-28: every request is one self-contained POST, with no `initialize`, no
sessions and no server-to-client channel. This front serves `server/discover`,
`tools/list`, `tools/call` and the empty prompt and resource lists, answers a
notification with 202, and refuses any older protocol version with -32022.
Stdio clients keep classic MCP through the shim (`tcp_transport.py`), served
from the same `core.Core`.

Before any MCP handling, a request whose `Host` is not this server's loopback
address, or whose `Origin` is set to anything else, gets 403, which blocks DNS
rebinding from a browser. Every route but `GET /health` needs a bearer token:
`/mcp` the per-user token from `shim.http_token`, `/shutdown` this server's own
state-file token. A missing or wrong token gets 403, not 401: a 401 starts
Claude Code's OAuth flow, which this server does not offer.

Claude Code's `headersHelper` (`mcp/launch/kg_headers.py`) runs in the plugin
folder and cannot see the session's cwd, so it sends `X-Codebase-KG-Client`, a
random id per connect, and `X-Codebase-KG-Graph` when `CODEBASE_KG_PATH` is set.
The first tool call from an unseen client gets an `input_required` result asking
for roots; the retry carries them in `inputResponses`, and the first `file:`
root becomes that client's cwd, cached so later calls cost no extra round trip.
A client that sends `X-Codebase-KG-Cwd` skips the roots request. Header values
are percent-encoded UTF-8.

A request body may be at most `MAX_BODY` bytes, sent with `Content-Length` or
chunked. A keep-alive connection that sends nothing for `KEEPALIVE_TIMEOUT`
seconds is closed, so idle clients do not hold threads.
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import socket
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from . import core, resolve, shim

logger = logging.getLogger("codebase_kg.http")

MODERN_PROTOCOL = "2026-07-28"
ROOTS_REQUEST = "codebase-kg-roots"
CLIENT_HEADER = "x-codebase-kg-client"
CWD_HEADER = "x-codebase-kg-cwd"
GRAPH_HEADER = "x-codebase-kg-graph"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022
STATUS = {core.PARSE_ERROR: 400, core.INVALID_REQUEST: 400, core.INVALID_PARAMS: 400,
          HEADER_MISMATCH: 400, UNSUPPORTED_VERSION: 400, core.METHOD_NOT_FOUND: 404}
NOT_ONE_MESSAGE = "Body must be a single JSON-RPC request or notification object"
MAX_CLIENTS = 1024
MAX_BODY = 8 * 1024 * 1024
KEEPALIVE_TIMEOUT = 60.0
HANDOVER_TIMEOUT = 5.0
LISTED = {"ttlMs": 0, "cacheScope": "private"}


class BodyError(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def root_path(responses: object) -> Path | None:
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


class Clients:
    """Each HTTP client's `resolve.Connection`, bound the way the shim handshake binds one."""

    def __init__(self, limit: int = MAX_CLIENTS) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self.bound: OrderedDict[tuple[str, str | None], resolve.Connection] = OrderedDict()

    def connection(
        self, headers: Mapping[str, str | None], params: dict[str, Any],
    ) -> resolve.Connection | dict[str, Any]:
        """The client's connection, or the result to answer with: an error or the roots request."""
        graph, cwd, client = (_decoded(headers.get(h)) for h in (GRAPH_HEADER, CWD_HEADER, CLIENT_HEADER))
        if cwd is not None:
            if not Path(cwd).is_absolute():
                return core.error_result("codebase-kg: X-Codebase-KG-Cwd must be an absolute path.")
            return _new(Path(cwd), graph)
        key = (client, graph) if client else None
        with self._lock:
            if key is not None and key in self.bound:
                self.bound.move_to_end(key)
                return self.bound[key]
        root = root_path(params.get("inputResponses"))
        if root is None:
            return {"resultType": "input_required", "inputRequests": {ROOTS_REQUEST: {"method": "roots/list"}}}
        conn = _new(root, graph)
        if key is not None:
            with self._lock:
                self.bound[key] = conn
                while len(self.bound) > self.limit:
                    self.bound.popitem(last=False)
        return conn


def _decoded(value: str | None) -> str | None:
    value = (value or "").strip()
    return unquote(value) if value else None


def _new(cwd: Path, graph: str | None) -> resolve.Connection:
    return resolve.Connection(cwd=cwd, explicit=Path(cwd, graph) if graph else None)


def _authorized(value: str, expected: str) -> bool:
    scheme, _, token = value.partition(" ")
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), expected)


class HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(
        self, sock: socket.socket, engine: core.Core, token: str, shutdown_token: str, health: dict[str, Any],
        on_request: Callable[[], None], on_shutdown: Callable[[], None],
    ) -> None:
        super().__init__(sock.getsockname(), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        sock.setblocking(True)
        port = sock.getsockname()[1]
        self.core = engine
        self.clients = Clients()
        self.token = token
        self.shutdown_token = shutdown_token
        self.health = health
        self.on_request = on_request
        self.on_shutdown = on_shutdown
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], OSError):
            logger.exception("HTTP request from %s failed", client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = KEEPALIVE_TIMEOUT

    @property
    def srv(self) -> HttpServer:
        return cast(HttpServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str | None = "application/json",
              headers: dict[str, str] | None = None) -> None:
        self.send_response_only(status)
        if content_type and body:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, core.dumps(obj).encode("utf-8"))

    def _refuse(self, status: int, error: str) -> None:
        self.close_connection = True
        self._json(status, {"error": error})

    def _rpc_error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._json(STATUS.get(code, 200), {"jsonrpc": "2.0", "id": request_id, "error": error})

    def _gate(self) -> bool:
        """Host, Origin, `/health`, the token and `/shutdown`. True when the request may reach `/mcp`."""
        srv = self.srv
        if self.headers.get("host", "").lower() not in srv.hosts:
            self._refuse(403, "Host is not this server")
            return False
        origin = self.headers.get("origin")
        if origin is not None and origin.lower() not in srv.origins:
            self._refuse(403, "Origin not allowed")
            return False
        path = self.path.split("?", 1)[0]
        if path == "/health" and self.command == "GET":
            self._json(200, srv.health)
            return False
        shutdown = path == "/shutdown"
        expected = srv.shutdown_token if shutdown else srv.token
        if not _authorized(self.headers.get("authorization", ""), expected):
            self._refuse(403, "missing or wrong bearer token")
            return False
        if shutdown:
            if self.command != "POST":
                self._refuse(405, "use POST")
                return False
            self.close_connection = True
            self._json(200, {"ok": True})
            srv.on_shutdown()
            return False
        if path != "/mcp":
            self.close_connection = True
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return False
        if self.command != "POST":
            self.close_connection = True
            self._send(405, headers={"Allow": "POST"})
            return False
        return True

    def do_GET(self) -> None:
        self._gate()

    do_DELETE = do_PUT = do_PATCH = do_GET

    def _body(self) -> bytes:
        chunked = "chunked" in self.headers.get("transfer-encoding", "").lower()
        length = self.headers.get("content-length")
        if chunked and length is not None:
            raise BodyError(400)
        if chunked:
            return self._chunks()
        try:
            size = int(length or 0)
        except ValueError:
            raise BodyError(400) from None
        if size < 0:
            raise BodyError(400)
        if size > MAX_BODY:
            raise BodyError(413)
        body = self.rfile.read(size)
        if len(body) != size:
            raise BodyError(400)
        return body

    def _chunks(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise BodyError(400) from None
            if size < 0:
                raise BodyError(400)
            if size == 0:
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(body)
            if len(body) + size > MAX_BODY:
                raise BodyError(413)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.readline(3).strip():
                raise BodyError(400)
            body += chunk

    def do_POST(self) -> None:
        if not self._gate():
            return
        self.srv.on_request()
        accept = self.headers.get("accept", "")
        if "application/json" not in accept and "*/*" not in accept:
            self.close_connection = True
            self._send(406)
            return
        try:
            body = self._body()
        except BodyError as exc:
            self.close_connection = True
            self._send(exc.status)
            return
        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):
            self._rpc_error(None, core.PARSE_ERROR, "Parse error")
            return
        self._message(msg)

    def _message(self, msg: Any) -> None:
        version = self.headers.get("mcp-protocol-version")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            self._rpc_error(None, core.INVALID_REQUEST, NOT_ONE_MESSAGE)
            return
        if "id" not in msg:
            if version != MODERN_PROTOCOL:
                self._unsupported(None, version or "")
            else:
                self._send(202, content_type=None)
            return
        request_id = msg["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            self._rpc_error(None, core.INVALID_REQUEST, NOT_ONE_MESSAGE)
            return
        if version is None or version in core.CLASSIC_VERSIONS:
            self._unsupported(request_id, version or "")
            return
        for name in ("mcp-protocol-version", "mcp-method", "mcp-name"):
            if len(self.headers.get_all(name) or []) > 1:
                self._rpc_error(request_id, HEADER_MISMATCH, f"{name} header appears more than once")
                return
        params = msg.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(params, dict) or not isinstance(meta, dict) or VERSION_KEY not in meta \
                or CAPABILITIES_KEY not in meta:
            self._rpc_error(request_id, core.INVALID_PARAMS, (
                f"params._meta must be an object carrying the required {VERSION_KEY!r} and "
                f"{CAPABILITIES_KEY!r} envelope keys"))
            return
        method = msg["method"]
        if version != meta[VERSION_KEY]:
            self._rpc_error(request_id, HEADER_MISMATCH,
                            "mcp-protocol-version header does not match the request envelope's protocol version")
            return
        if self.headers.get("mcp-method") != method:
            self._rpc_error(request_id, HEADER_MISMATCH, "mcp-method header does not match the request body's method")
            return
        if method == "tools/call" and params.get("name") is not None \
                and unquote(self.headers.get("mcp-name") or "") != params.get("name"):
            self._rpc_error(request_id, HEADER_MISMATCH,
                            "mcp-name header does not match the request body's 'name' parameter")
            return
        if version != MODERN_PROTOCOL:
            self._unsupported(request_id, version)
            return
        result = self._dispatch(method, params)
        if result is None:
            self._rpc_error(request_id, core.METHOD_NOT_FOUND, "Method not found", method)
            return
        result.setdefault("resultType", "complete")
        result.setdefault("_meta", {SERVER_INFO_KEY: core.SERVER_INFO})
        self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def _unsupported(self, request_id: Any, requested: str) -> None:
        self._rpc_error(request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                        {"supported": [MODERN_PROTOCOL], "requested": requested})

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "server/discover":
            return {**LISTED, "supportedVersions": [MODERN_PROTOCOL],
                    "capabilities": {"tools": {"listChanged": False}}, "instructions": core.INSTRUCTIONS}
        if method == "tools/list":
            return {"tools": core.CATALOG["tools"], **LISTED}
        if method in core.EMPTY_LISTS:
            return {core.EMPTY_LISTS[method]: [], **LISTED}
        if method == "tools/call":
            headers = {h: self.headers.get(h) for h in (GRAPH_HEADER, CWD_HEADER, CLIENT_HEADER)}
            found = self.srv.clients.connection(headers, params)
            if not isinstance(found, resolve.Connection):
                return found
            return self.srv.core.call_tool(found, params.get("name"), params.get("arguments"))
        return None


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

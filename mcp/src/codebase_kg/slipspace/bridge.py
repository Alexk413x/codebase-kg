"""A stdio MCP server that is a bridge to one HTTP MCP endpoint on this machine. Stdlib only.

A stdio client starts one bridge per session. The bridge reads newline-delimited
JSON-RPC on stdin, sends each request as its own POST to the endpoint, and writes
the answer to stdout. It always speaks MCP 2026-07-28 to the server, so the
server needs one dispatcher for every client.

A client that already speaks 2026-07-28 (its requests carry the protocol version
in `params._meta`) passes through: the bridge adds the HTTP headers that mirror
the body and changes nothing else.

A client that opens with `initialize` is translated, and this module is the only
place that does it:

- `initialize` is answered from the server's `server/discover` result.
- `notifications/initialized` is absorbed and `ping` is answered here.
- Every other request gets the 2026-07-28 envelope, built from what the client
  declared in `initialize`.
- A result of type `input_required` becomes requests to the client (`roots/list`,
  `elicitation/create`, `sampling/createMessage`), which is how those revisions
  ask. The bridge then repeats the call with the client's answers.

Requests run at the same time, each on its own thread and connection. A
`notifications/cancelled` from the client closes the matching connection, which is
the cancel signal of the HTTP transport, and the bridge writes no result for it.

Connections are kept open and reused for up to `KEEP` seconds. The owner's
headers function runs once for each new connection and never for a kept one, so
an owner that checks who holds the port before it hands over a credential checks
every process the credential is sent to: a kept connection stays with the process
that was checked.

When the server cannot be reached, the bridge asks its owner to start it and
tries once more. When the server refuses the token, it opens a new connection,
which asks the owner for the headers again, and tries once more. A request that
fails on a kept connection is sent once more on a new one, because a server that
closed the connection while it was idle is the common cause. A request that fails
on a new connection after it was sent is never retried, because the server may
have run the call: the client gets a JSON-RPC error and decides.

This folder imports nothing from the package around it. The owner passes the
port, a function that returns the request headers, and a function that starts
the server.
"""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
import time
from typing import Any, BinaryIO, Callable

MODERN = "2026-07-28"
CLASSIC_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
MODERN_ONLY_RESULT_KEYS = ("resultType", "ttlMs", "cacheScope")
NAMED_BY = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
UNAVAILABLE = -32000
INTERNAL = -32603
REQUEST_TIMEOUT = 300.0
CLIENT_ANSWER_TIMEOUT = 600.0
MAX_ROUNDS = 8
KEEP = 20.0
MAX_KEPT = 8
DRAIN = 5.0

Headers = Callable[[bool], "dict[str, str]"]


class Unavailable(Exception):
    """The server gave no usable answer."""


class Cancelled(Exception):
    """The client cancelled the request, or stdin closed."""


def header_value(text: str) -> str:
    """A header value for `Mcp-Name`: plain when it is safe ASCII, else the Base64 form of the specification."""
    if text and text == text.strip() and all(0x20 <= ord(ch) < 0x7F for ch in text) and not text.startswith("=?"):
        return text
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def events(body: bytes) -> list[Any]:
    """The JSON payloads of a `text/event-stream` body, in order."""
    out: list[Any] = []
    data: list[str] = []
    for line in body.decode("utf-8", "replace").splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            try:
                out.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return out


class Bridge:
    def __init__(
        self, port: int, headers: Headers, ensure: Callable[[], bool],
        reader: BinaryIO | None = None, writer: BinaryIO | None = None,
        host: str = "127.0.0.1", path: str = "/mcp", name: str = "the MCP server",
    ) -> None:
        self.port = port
        self.host = host
        self.path = path
        self.name = name
        self._headers = headers
        self._ensure = ensure
        self._reader = reader if reader is not None else sys.stdin.buffer
        self._writer = writer if writer is not None else sys.stdout.buffer
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._inflight: dict[Any, http.client.HTTPConnection | None] = {}
        self._cancelled: set[Any] = set()
        self._waiters: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._threads: set[threading.Thread] = set()
        self._idle: list[tuple[http.client.HTTPConnection, dict[str, str], float]] = []
        self._asked = 0
        self._closing = threading.Event()
        self.client_capabilities: dict[str, Any] = {}
        self.client_info: dict[str, Any] | None = None

    def run(self) -> int:
        while True:
            try:
                line = self._reader.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            if line.strip():
                self._line(line)
        self._closing.set()
        with self._lock:
            waiters = list(self._waiters.values())
            threads = list(self._threads)
        for event, _ in waiters:
            event.set()
        for thread in threads:
            thread.join(DRAIN)
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _, _ in idle:
            conn.close()
        return 0

    def _line(self, line: bytes) -> None:
        try:
            parsed = json.loads(line)
        except ValueError:
            self._write(error(None, -32700, "Parse error"))
            return
        for message in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(message, dict):
                self._message(message)
            else:
                self._write(error(None, -32600, "Invalid Request"))

    def _message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            self._answer_from_client(message)
        elif "id" not in message:
            if method == "notifications/cancelled":
                params = message.get("params")
                self._cancel(params.get("requestId") if isinstance(params, dict) else None)
        else:
            thread = threading.Thread(target=self._serve, args=(message,), daemon=True)
            with self._lock:
                self._inflight[message["id"]] = None
                self._threads.add(thread)
            thread.start()

    def _answer_from_client(self, message: dict[str, Any]) -> None:
        with self._lock:
            waiter = self._waiters.get(str(message.get("id")))
        if waiter is not None:
            waiter[1].append(message)
            waiter[0].set()

    def _cancel(self, request_id: Any) -> None:
        with self._lock:
            if request_id not in self._inflight:
                return
            self._cancelled.add(request_id)
            conn = self._inflight[request_id]
            waiters = list(self._waiters.values())
        if conn is not None:
            _hang_up(conn)
        for event, _ in waiters:
            event.set()

    def _serve(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        try:
            try:
                reply = self._reply(message)
            except Cancelled:
                return
            except Unavailable as exc:
                reply = error(request_id, UNAVAILABLE, f"{self.name} is unavailable ({exc}); retry the call")
            except Exception as exc:  # noqa: BLE001 - a bridge fault must still answer the request
                reply = error(request_id, INTERNAL, f"bridge error: {exc}")
            with self._lock:
                cancelled = request_id in self._cancelled
            if not cancelled:
                self._write(reply)
        finally:
            with self._lock:
                self._inflight.pop(request_id, None)
                self._cancelled.discard(request_id)
                self._threads.discard(threading.current_thread())

    def _reply(self, message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict) and VERSION_KEY in meta:
            return self._post(message, message["id"])
        return self._classic(message)

    # --- the older handshake, translated ------------------------------------------------------------

    def _classic(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id, method = message["id"], message["method"]
        raw = message.get("params")
        params: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        for _ in range(MAX_ROUNDS):
            reply = self._post({"jsonrpc": "2.0", "id": request_id, "method": method,
                                "params": self._enveloped(params)}, request_id)
            result = reply.get("result")
            if not isinstance(result, dict):
                return reply
            if result.get("resultType") != "input_required":
                for key in MODERN_ONLY_RESULT_KEYS:
                    result.pop(key, None)
                return reply
            answers: dict[str, Any] = {}
            requests = result.get("inputRequests")
            for key, ask in (requests if isinstance(requests, dict) else {}).items():
                answer = self._ask_client(request_id, ask if isinstance(ask, dict) else {})
                if "result" not in answer:
                    failure = answer.get("error")
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": failure if isinstance(failure, dict) else {"code": INTERNAL, "message": "no answer"}}
                answers[key] = answer["result"]
            params["inputResponses"] = answers
            if "requestState" in result:
                params["requestState"] = result["requestState"]
            else:
                params.pop("requestState", None)
        return error(request_id, INTERNAL, f"{self.name} asked for input {MAX_ROUNDS} times without finishing")

    def _enveloped(self, params: dict[str, Any]) -> dict[str, Any]:
        own = params.get("_meta")
        meta: dict[str, Any] = dict(own) if isinstance(own, dict) else {}
        meta[VERSION_KEY] = MODERN
        meta[CAPABILITIES_KEY] = self.client_capabilities
        if self.client_info is not None:
            meta[CLIENT_INFO_KEY] = self.client_info
        return {**params, "_meta": meta}

    def _initialize(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        capabilities = params.get("capabilities")
        self.client_capabilities = capabilities if isinstance(capabilities, dict) else {}
        info = params.get("clientInfo")
        self.client_info = info if isinstance(info, dict) else None
        reply = self._post({"jsonrpc": "2.0", "id": request_id, "method": "server/discover",
                            "params": self._enveloped({})}, request_id)
        found = reply.get("result")
        if not isinstance(found, dict):
            return reply
        asked = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": asked if asked in CLASSIC_VERSIONS else CLASSIC_VERSIONS[-1],
            "capabilities": found.get("capabilities") or {},
            "serverInfo": (found.get("_meta") or {}).get(SERVER_INFO_KEY) or {"name": self.name, "version": "0"},
        }
        if isinstance(found.get("instructions"), str):
            result["instructions"] = found["instructions"]
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _ask_client(self, request_id: Any, ask: dict[str, Any]) -> dict[str, Any]:
        """Send the server's input request to the client as a JSON-RPC request and return the client's response."""
        event = threading.Event()
        box: list[dict[str, Any]] = []
        with self._lock:
            self._asked += 1
            ask_id = f"bridge-{self._asked}"
            self._waiters[ask_id] = (event, box)
        try:
            request: dict[str, Any] = {"jsonrpc": "2.0", "id": ask_id, "method": ask.get("method")}
            if "params" in ask:
                request["params"] = ask["params"]
            self._write(request)
            waited = 0.0
            while not box:
                with self._lock:
                    stop = request_id in self._cancelled
                if stop or self._closing.is_set() or waited >= CLIENT_ANSWER_TIMEOUT:
                    raise Cancelled
                event.wait(1.0)
                event.clear()
                waited += 1.0
            return box[0]
        finally:
            with self._lock:
                self._waiters.pop(ask_id, None)

    # --- one request to the server --------------------------------------------------------------------

    def _post(self, message: dict[str, Any], request_id: Any) -> dict[str, Any]:
        """The server's JSON-RPC response to `message`. Raises `Unavailable` or `Cancelled`."""
        params = message.get("params")
        fields: dict[str, Any] = params if isinstance(params, dict) else {}
        mirrored = {"MCP-Protocol-Version": MODERN, "Mcp-Method": str(message.get("method")),
                    "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                    "Host": f"{self.host}:{self.port}"}
        named = fields.get(NAMED_BY.get(str(message.get("method")), ""))
        if isinstance(named, str):
            mirrored["Mcp-Name"] = header_value(named)
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        started = refused = stale = False
        while True:
            kept = self._take()
            if kept is not None:
                conn, owned = kept
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=REQUEST_TIMEOUT)
                try:
                    conn.connect()
                except OSError as exc:
                    conn.close()
                    if started or not self._ensure():
                        raise Unavailable(f"no server on port {self.port}") from exc
                    started = True
                    continue
                owned = self._headers(started or refused)
            with self._lock:
                if request_id in self._cancelled:
                    conn.close()
                    raise Cancelled
                self._inflight[request_id] = conn
            try:
                conn.request("POST", self.path, body=body, headers={**owned, **mirrored})
                response = conn.getresponse()
                raw = response.read()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                with self._lock:
                    self._inflight[request_id] = None
                    if request_id in self._cancelled:
                        raise Cancelled from exc
                if kept is not None and not stale:
                    stale = True
                    continue
                raise Unavailable("the connection closed before the answer") from exc
            with self._lock:
                self._inflight[request_id] = None
            if response.status == 403 and not refused:
                conn.close()
                refused = True
                self._ensure()
                continue
            if response.will_close or response.status == 403:
                conn.close()
            else:
                self._give(conn, owned)
            return self._parsed(response, raw, request_id)

    def _take(self) -> tuple[http.client.HTTPConnection, dict[str, str]] | None:
        """A kept connection that is still fresh, with the headers it was opened with."""
        while True:
            with self._lock:
                if not self._idle:
                    return None
                conn, owned, since = self._idle.pop()
            if time.monotonic() - since < KEEP:
                return conn, owned
            conn.close()

    def _give(self, conn: http.client.HTTPConnection, owned: dict[str, str]) -> None:
        with self._lock:
            if len(self._idle) < MAX_KEPT and not self._closing.is_set():
                self._idle.append((conn, owned, time.monotonic()))
                return
        conn.close()

    def _parsed(self, response: http.client.HTTPResponse, raw: bytes, request_id: Any) -> dict[str, Any]:
        if "text/event-stream" in (response.getheader("content-type") or ""):
            final: dict[str, Any] | None = None
            for item in events(raw):
                if isinstance(item, dict) and "method" in item and "id" not in item:
                    self._write(item)
                elif isinstance(item, dict):
                    final = item
            if final is None:
                raise Unavailable("the stream ended without a response")
            return final
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed) and parsed.get("jsonrpc") == "2.0":
            if parsed.get("id") is None:
                parsed["id"] = request_id
            return parsed
        raise Unavailable(f"HTTP {response.status}")

    def _write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                self._writer.write(data)
                self._writer.flush()
            except (OSError, ValueError):
                self._closing.set()


def _hang_up(conn: http.client.HTTPConnection) -> None:
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(2)
        except OSError:
            pass
    conn.close()

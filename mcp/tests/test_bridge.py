"""The stdio-to-HTTP bridge: one stdio session served from the shared server's HTTP endpoint.

The first part drives `slipspace.bridge.Bridge` against a scripted HTTP endpoint. The second part
launches the shim in bridge mode against the real server, the way a stdio client launches it.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import http_transport, shim
from codebase_kg.slipspace import bridge
from test_shared_server import (
    Client,
    _client,
    _close,
    _finish,
    _kill_daemon,
    _repo,
    _shim,
)

V = bridge.MODERN
META = {bridge.VERSION_KEY: V, bridge.CAPABILITIES_KEY: {"roots": {}}}
Answer = Callable[[dict[str, Any], dict[str, str]], Any]


def _ok(request: dict[str, Any], result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"jsonrpc": "2.0", "id": request["id"], "result": {"resultType": "complete", **result}}


class Endpoint:
    """A scripted `/mcp` endpoint. `answer` returns (status, json body), or a callable that writes the response."""

    def __init__(self, answer: Answer, port: int = 0) -> None:
        self.answer = answer
        self.seen: list[tuple[dict[str, Any], dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.seen.append((body, headers))
                reply = outer.answer(body, headers)
                if callable(reply):
                    reply(self)
                    return
                status, payload = reply
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def methods(self) -> list[str]:
        return [body["method"] for body, _ in self.seen]


class Session:
    """A bridge on pipes, with the client's end of them."""

    def __init__(self, port: int, ensure: Callable[[], bool] | None = None) -> None:
        to_bridge_r, to_bridge_w = os.pipe()
        from_bridge_r, from_bridge_w = os.pipe()
        self.refreshes: list[bool] = []
        self.ensured = 0
        self._ensure = ensure

        def headers(refresh: bool) -> dict[str, str]:
            self.refreshes.append(refresh)
            return {"Authorization": f"Bearer token-{sum(self.refreshes)}", "X-Test-Cwd": "C%3A/work"}

        def ensure_it() -> bool:
            self.ensured += 1
            return self._ensure() if self._ensure else False

        self._stdin = os.fdopen(to_bridge_w, "wb", buffering=0)
        self.client = Client(self._stdin, os.fdopen(from_bridge_r, "rb", buffering=0))
        self.bridge = bridge.Bridge(port, headers, ensure_it, reader=os.fdopen(to_bridge_r, "rb", buffering=0),
                                    writer=os.fdopen(from_bridge_w, "wb", buffering=0), name="the test server")
        self.code: list[int] = []
        self.thread = threading.Thread(target=lambda: self.code.append(self.bridge.run()), daemon=True)
        self.thread.start()

    def close(self) -> int:
        self._stdin.close()
        self.thread.join(15)
        assert self.code, "the bridge did not stop at end of input"
        return self.code[0]


@pytest.fixture
def serve() -> Iterator[Callable[..., tuple[Endpoint, Session]]]:
    made: list[tuple[Endpoint, Session]] = []

    def _serve(answer: Answer, ensure: Callable[[], bool] | None = None) -> tuple[Endpoint, Session]:
        endpoint = Endpoint(answer)
        session = Session(endpoint.port, ensure)
        made.append((endpoint, session))
        return endpoint, session

    yield _serve
    for endpoint, session in made:
        if not session.code:
            session.close()
        endpoint.close()


def _modern(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**(params or {}), "_meta": META}


# --- a client that already speaks 2026-07-28 -------------------------------------------------------
def test_a_modern_request_passes_through_with_the_mirrored_headers(serve: Any) -> None:
    endpoint, session = serve(lambda request, headers: _ok(request, {"content": [], "isError": False}))
    params = _modern("tools/call", {"name": "kg_search", "arguments": {"query": "x"}})

    reply = session.client.request("tools/call", params)

    assert reply["result"] == {"resultType": "complete", "content": [], "isError": False}
    body, headers = endpoint.seen[0]
    assert body == {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    assert headers["mcp-protocol-version"] == V and headers["mcp-method"] == "tools/call"
    assert headers["mcp-name"] == "kg_search"
    assert headers["authorization"] == "Bearer token-0" and headers["x-test-cwd"] == "C%3A/work"
    assert "application/json" in headers["accept"] and "text/event-stream" in headers["accept"]


def test_a_modern_input_required_result_is_the_clients_to_answer(serve: Any) -> None:
    asked = {"resultType": "input_required", "inputRequests": {"r": {"method": "roots/list"}}}
    endpoint, session = serve(lambda request, headers: (200, {"jsonrpc": "2.0", "id": request["id"], "result": asked}))
    assert session.client.request("tools/call", _modern("tools/call", {"name": "t"}))["result"] == asked
    assert len(endpoint.seen) == 1


def test_a_json_rpc_error_with_an_http_error_status_reaches_the_client(serve: Any) -> None:
    failure = {"code": -32602, "message": "Unknown tool: nope"}
    _, session = serve(lambda request, headers: (400, {"jsonrpc": "2.0", "id": request["id"], "error": failure}))
    assert session.client.request("tools/call", _modern("tools/call", {"name": "nope"}))["error"] == failure


@pytest.mark.parametrize(("name", "expected"), [
    ("kg_search", "kg_search"),
    ("naïve tool", "=?base64?" + base64.b64encode("naïve tool".encode()).decode() + "?="),
    (" padded ", "=?base64?" + base64.b64encode(b" padded ").decode() + "?="),
    ("=?looks-encoded", "=?base64?" + base64.b64encode(b"=?looks-encoded").decode() + "?="),
])
def test_the_name_header_is_plain_ascii_or_the_base64_form(name: str, expected: str) -> None:
    assert bridge.header_value(name) == expected


# --- a client that opens with initialize ----------------------------------------------------------------
DISCOVERED = {
    "supportedVersions": [V], "capabilities": {"tools": {"listChanged": False}}, "instructions": "use the graph",
    "ttlMs": 0, "cacheScope": "private", "_meta": {bridge.SERVER_INFO_KEY: {"name": "codebase-kg", "version": "9"}},
}
INIT = {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {"form": {}}},
        "clientInfo": {"name": "pytest", "version": "0"}}


def _classic_endpoint(tool: Callable[[dict[str, Any]], dict[str, Any]]) -> Answer:
    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        if request["method"] == "server/discover":
            return _ok(request, DISCOVERED)
        return 200, {"jsonrpc": "2.0", "id": request["id"], "result": tool(request)}

    return answer


def test_initialize_is_answered_from_discover(serve: Any) -> None:
    endpoint, session = serve(_classic_endpoint(lambda request: {"resultType": "complete"}))

    result = session.client.request("initialize", INIT)["result"]

    assert result == {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "codebase-kg", "version": "9"}, "instructions": "use the graph"}
    body, headers = endpoint.seen[0]
    assert body["method"] == "server/discover" and headers["mcp-method"] == "server/discover"
    assert body["params"]["_meta"] == {bridge.VERSION_KEY: V, bridge.CAPABILITIES_KEY: INIT["capabilities"],
                                       bridge.CLIENT_INFO_KEY: INIT["clientInfo"]}


@pytest.mark.parametrize(("asked", "agreed"), [("2024-11-05", "2024-11-05"), ("2025-11-25", "2025-11-25"),
                                                ("2099-01-01", "2025-11-25"), (V, "2025-11-25")])
def test_initialize_agrees_only_on_a_version_from_before_the_stateless_revision(
    serve: Any, asked: str, agreed: str,
) -> None:
    _, session = serve(_classic_endpoint(lambda request: {"resultType": "complete"}))
    assert session.client.request("initialize", {**INIT, "protocolVersion": asked})["result"]["protocolVersion"] == agreed


def test_the_initialized_notification_and_ping_never_reach_the_server(serve: Any) -> None:
    endpoint, session = serve(_classic_endpoint(lambda request: {"resultType": "complete"}))
    session.client.initialize()
    assert session.client.request("ping", {}) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert endpoint.methods() == ["server/discover"]


def test_a_classic_call_gets_the_envelope_and_loses_the_stateless_only_fields(serve: Any) -> None:
    endpoint, session = serve(_classic_endpoint(
        lambda request: {"resultType": "complete", "ttlMs": 0, "cacheScope": "private", "tools": [{"name": "t"}]}))
    session.client.request("initialize", INIT)

    reply = session.client.request("tools/list", {"_meta": {"progressToken": 7}})

    assert reply == {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "t"}]}}
    body, headers = endpoint.seen[-1]
    assert headers["mcp-protocol-version"] == V
    assert body["params"]["_meta"] == {"progressToken": 7, bridge.VERSION_KEY: V,
                                       bridge.CAPABILITIES_KEY: INIT["capabilities"],
                                       bridge.CLIENT_INFO_KEY: INIT["clientInfo"]}


def test_a_call_before_initialize_declares_no_capabilities(serve: Any) -> None:
    endpoint, session = serve(_classic_endpoint(lambda request: {"resultType": "complete", "tools": []}))
    session.client.request("tools/list", {})
    assert endpoint.seen[0][0]["params"]["_meta"] == {bridge.VERSION_KEY: V, bridge.CAPABILITIES_KEY: {}}


FORM = {"mode": "form", "message": "Skip the graph update?",
        "requestedSchema": {"type": "object", "properties": {"approve": {"type": "boolean"}}}}


def _elicits(request: dict[str, Any]) -> dict[str, Any]:
    params = request["params"]
    if "inputResponses" not in params:
        return {"resultType": "input_required", "requestState": "opaque-1",
                "inputRequests": {"confirm": {"method": "elicitation/create", "params": FORM}}}
    return {"resultType": "complete", "isError": False, "content": [{"type": "text", "text": json.dumps(
        {"answers": params["inputResponses"], "state": params.get("requestState")}, sort_keys=True)}]}


def test_an_input_request_reaches_a_classic_client_as_a_request_and_its_answer_goes_back(serve: Any) -> None:
    """The gate of R19: an elicitation the server raises must reach a client that speaks 2025-06-18."""
    endpoint, session = serve(_classic_endpoint(_elicits))
    client = session.client
    client.request("initialize", INIT)
    client.send({"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "ask", "arguments": {}}})

    asked = client.recv()
    assert asked["method"] == "elicitation/create" and asked["params"] == FORM and "result" not in asked
    client.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"action": "accept", "content": {"approve": True}}})
    final = client.recv()

    assert final["id"] == 50 and "resultType" not in final["result"]
    assert json.loads(final["result"]["content"][0]["text"]) == {
        "answers": {"confirm": {"action": "accept", "content": {"approve": True}}}, "state": "opaque-1"}
    retried = endpoint.seen[-1][0]
    assert retried["method"] == "tools/call" and retried["params"]["name"] == "ask"
    assert endpoint.methods() == ["server/discover", "tools/call", "tools/call"]


def test_a_client_that_refuses_the_input_request_fails_the_call(serve: Any) -> None:
    _, session = serve(_classic_endpoint(_elicits))
    client = session.client
    client.request("initialize", INIT)
    client.send({"jsonrpc": "2.0", "id": 51, "method": "tools/call", "params": {"name": "ask"}})
    asked = client.recv()
    client.send({"jsonrpc": "2.0", "id": asked["id"], "error": {"code": -32601, "message": "no elicitation"}})
    assert client.recv() == {"jsonrpc": "2.0", "id": 51, "error": {"code": -32601, "message": "no elicitation"}}


def test_a_server_that_never_stops_asking_ends_in_an_error(serve: Any) -> None:
    _, session = serve(_classic_endpoint(
        lambda request: {"resultType": "input_required", "inputRequests": {"r": {"method": "roots/list"}}}))
    client = session.client
    client.send({"jsonrpc": "2.0", "id": 52, "method": "tools/call", "params": {"name": "loop"}})
    for _ in range(bridge.MAX_ROUNDS):
        asked = client.recv()
        assert asked["method"] == "roots/list"
        client.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"roots": []}})
    assert client.recv()["error"]["code"] == bridge.INTERNAL


# --- overlapping requests, cancellation, restarts -------------------------------------------------------
def _slow_then_fast(delay: float) -> Answer:
    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        if request["params"].get("name") == "slow":
            time.sleep(delay)
        return _ok(request, {"name": request["params"].get("name")})

    return answer


def test_a_slow_call_does_not_hold_up_the_next_request(serve: Any) -> None:
    _, session = serve(_slow_then_fast(1.5))
    client = session.client
    client.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": _modern("", {"name": "slow"})})
    client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": _modern("", {"name": "fast"})})
    assert [client.recv()["id"], client.recv()["id"]] == [2, 1]


def test_a_cancelled_request_gets_no_answer_and_its_connection_closes(serve: Any) -> None:
    closed = threading.Event()

    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        if request["params"].get("name") != "slow":
            return _ok(request, {"name": "fast"})

        def hold(handler: BaseHTTPRequestHandler) -> None:
            handler.connection.settimeout(10)
            try:
                if handler.connection.recv(1) == b"":
                    closed.set()
            except OSError:
                closed.set()

        return hold

    _, session = serve(answer)
    client = session.client
    client.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": _modern("", {"name": "slow"})})
    time.sleep(0.5)
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})

    assert closed.wait(5), "the bridge kept the cancelled request's connection open"
    assert client.request("tools/call", _modern("", {"name": "fast"}))["result"]["name"] == "fast"
    assert [m.get("id") for m in client.seen] == [1], "only the second request, numbered 1 by the client, is answered"


def test_a_refused_token_is_read_again_and_the_request_is_sent_once_more(serve: Any) -> None:
    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        if headers["authorization"] == "Bearer token-0":
            return 403, {"error": "missing or wrong bearer token"}
        return _ok(request, {"token": headers["authorization"]})

    endpoint, session = serve(answer, ensure=lambda: True)
    assert session.client.request("tools/list", _modern(""))["result"]["token"] == "Bearer token-1"
    assert session.refreshes == [False, True] and len(endpoint.seen) == 2


def test_a_token_refused_twice_is_an_error_not_a_loop(serve: Any) -> None:
    endpoint, session = serve(lambda request, headers: (403, {"error": "missing or wrong bearer token"}),
                              ensure=lambda: True)
    reply = session.client.request("tools/list", _modern(""))
    assert reply["error"]["code"] == bridge.UNAVAILABLE and "HTTP 403" in reply["error"]["message"]
    assert len(endpoint.seen) == 2


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_no_server_asks_the_owner_to_start_one_and_tries_once_more() -> None:
    port = _free_port()
    started: list[Endpoint] = []

    def ensure() -> bool:
        started.append(Endpoint(lambda request, headers: _ok(request, {"up": True}), port))
        return True

    session = Session(port, ensure)
    try:
        assert session.client.request("tools/list", _modern(""))["result"]["up"] is True
        assert session.ensured == 1 and session.refreshes == [False, True]
    finally:
        session.close()
        for endpoint in started:
            endpoint.close()


def test_a_server_that_cannot_start_is_an_error_for_the_request() -> None:
    session = Session(_free_port())
    try:
        reply = session.client.request("tools/list", _modern(""))
        assert reply["error"]["code"] == bridge.UNAVAILABLE and "retry the call" in reply["error"]["message"]
        assert session.ensured == 1
    finally:
        session.close()


def test_a_connection_lost_after_the_request_was_sent_is_an_error_and_is_not_sent_again(serve: Any) -> None:
    """The server may have run the call. Sending a write twice is worse than an error the client can act on."""
    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        def drop(handler: BaseHTTPRequestHandler) -> None:
            handler.close_connection = True
            handler.connection.shutdown(socket.SHUT_RDWR)

        return drop

    endpoint, session = serve(answer, ensure=lambda: True)
    reply = session.client.request("tools/call", _modern("", {"name": "kg_upsert_node"}))
    assert reply["error"]["code"] == bridge.UNAVAILABLE
    assert len(endpoint.seen) == 1 and session.ensured == 0


def test_a_stream_response_forwards_its_notifications_then_the_answer(serve: Any) -> None:
    def answer(request: dict[str, Any], headers: dict[str, str]) -> Any:
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": 1, "progress": 1}}
        final = {"jsonrpc": "2.0", "id": request["id"], "result": {"resultType": "complete", "done": True}}
        data = "".join(f"event: message\ndata: {json.dumps(item)}\n\n" for item in (progress, final)).encode()

        def stream(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)

        return stream

    _, session = serve(answer)
    assert session.client.request("tools/call", _modern("", {"name": "t"}))["result"]["done"] is True
    assert session.client.seen[0]["method"] == "notifications/progress"


def test_bad_lines_are_answered_and_end_of_input_stops_the_bridge(serve: Any) -> None:
    _, session = serve(lambda request, headers: _ok(request, {}))
    session.client.send({"not": "json-rpc"})
    session.client._write.write(b"{broken\n")
    assert session.client.recv()["error"]["code"] == -32700
    assert session.close() == 0


def test_the_bridge_folder_imports_nothing_from_the_package() -> None:
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "codebase_kg" not in source and "from ." not in source and "import shim" not in source


def test_the_shim_sends_the_header_names_the_server_reads() -> None:
    assert shim.CLIENT_HEADER.lower() == http_transport.CLIENT_HEADER
    assert shim.CWD_HEADER.lower() == http_transport.CWD_HEADER
    assert shim.GRAPH_HEADER.lower() == http_transport.GRAPH_HEADER


# --- the shim in bridge mode, against the real server -----------------------------------------------
@pytest.fixture
def bridged(tmp_path: Path) -> Iterator[Callable[..., Client]]:
    cache = tmp_path / "cache"
    port = str(_free_port())
    procs = []

    def start(cwd: Path, **env: str) -> Client:
        proc = _shim(cwd, cache, CODEBASE_KG_BRIDGE="1", CODEBASE_KG_PORT=port,
                     CODEBASE_KG_SESSION_GRACE="-1", **env)
        procs.append(proc)
        return _client(proc)

    start.cache = cache  # type: ignore[attr-defined]
    start.procs = procs  # type: ignore[attr-defined]
    yield start
    for proc in procs:
        _finish(proc)
    _kill_daemon(cache)


def test_a_classic_stdio_client_is_served_through_the_bridge(bridged: Any, tmp_path: Path) -> None:
    alpha, beta = _repo(tmp_path / "alpha", "alpha"), _repo(tmp_path / "beta", "beta")
    a, b = bridged(alpha), bridged(beta)

    init = a.initialize()["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "codebase-kg"
    assert init["capabilities"] == {"tools": {"listChanged": False}} and init["instructions"]
    listed = a.request("tools/list", {})["result"]
    assert len(listed["tools"]) == 13 and "resultType" not in listed

    b.initialize()
    assert [r["id"] for r in a.data("kg_search", query="widget")["results"]] == ["alpha_widget"]
    assert [r["id"] for r in b.data("kg_search", query="widget")["results"]] == ["beta_widget"]
    assert a.request("tools/call", {"name": "kg_nope", "arguments": {}})["error"]["code"] == -32602


def test_a_stdio_client_can_speak_the_stateless_revision_through_the_bridge(bridged: Any, tmp_path: Path) -> None:
    client = bridged(_repo(tmp_path / "alpha", "alpha"))
    found = client.request("server/discover", {"_meta": META})["result"]
    assert found["supportedVersions"] == [V] and found["resultType"] == "complete"
    result = client.request("tools/call", {"name": "kg_search", "arguments": {"query": "widget"},
                                           "_meta": {bridge.VERSION_KEY: V, bridge.CAPABILITIES_KEY: {}}})["result"]
    assert result["resultType"] == "complete"
    assert [r["id"] for r in result["structuredContent"]["results"]] == ["alpha_widget"]


def test_the_explicit_graph_path_reaches_the_server(bridged: Any, tmp_path: Path) -> None:
    alpha, beta = _repo(tmp_path / "alpha", "alpha"), _repo(tmp_path / "beta", "beta")
    client = bridged(alpha, CODEBASE_KG_PATH=str(beta / "knowledge" / "code_graph.db"))
    client.initialize()
    assert [r["id"] for r in client.data("kg_search", query="widget")["results"]] == ["beta_widget"]


def test_a_server_restart_costs_the_session_nothing(bridged: Any, tmp_path: Path) -> None:
    client = bridged(_repo(tmp_path / "alpha", "alpha"))
    client.initialize()
    assert client.data("kg_search", query="widget")["count"] == 1

    _kill_daemon(bridged.cache)
    deadline = time.monotonic() + 10
    while shim.read_state(shim.server_build()) is not None and time.monotonic() < deadline:
        time.sleep(0.1)

    assert client.data("kg_search", query="widget")["count"] == 1


def test_end_of_input_stops_the_bridged_shim(bridged: Any, tmp_path: Path) -> None:
    client = bridged(_repo(tmp_path / "alpha", "alpha"))
    client.initialize()
    assert _close(bridged.procs[0]) == 0

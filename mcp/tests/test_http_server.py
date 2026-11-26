"""The shared server over Streamable HTTP, its port handover, the start hook and the headers helper.

Claude Code speaks MCP 2026-07-28 over HTTP: no sessions, and a headersHelper
that cannot see the session's cwd. The server learns each client's cwd from its
roots, asked for once through an `InputRequiredResult`, and must never cross
two clients' graphs. These tests drive it with fastmcp's client, which answers
that request the way Claude Code does. `test_http_battery.py` covers the protocol
surface request by request.
"""

from __future__ import annotations

import asyncio
import http.client
import http.server
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from codebase_kg import core, http_transport, shim
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from test_shared_server import BUILD, TIMEOUT, _graph, _raw, _repo, _stop
from test_shared_server import Client as LineClient

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "kg_server_start.py"
HEADERS = ROOT / "mcp" / "launch" / "kg_headers.py"
LAUNCHER = '. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/'
SRC_SHIM = ROOT / "mcp" / "src" / "codebase_kg" / "shim.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(cache: Path, cwd: Path, **env: str) -> subprocess.Popen[bytes]:
    full = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache), "CODEBASE_KG_PORT": "0", **env}
    return subprocess.Popen(
        shim.server_command("--serve"),
        cwd=cwd, env=full, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _state(cache: Path) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    path = cache / f"server-{BUILD}.json"
    while time.monotonic() < deadline:
        # A server on a port of its own names its state file by build and port.
        for found in [path, *cache.glob(f"server-{BUILD}-p*.json")]:
            try:
                state = json.loads(found.read_text(encoding="utf-8"))
                if state.get("http_port"):
                    return state
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    raise AssertionError(f"no state with an HTTP port at {path}")


@pytest.fixture(scope="module")
def http_daemon(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("http")
    decoy = _repo(root / "decoy", "decoy")
    cache = root / "cache"
    proc = _start(cache, decoy, CODEBASE_KG_PATH=str(_graph(decoy)))
    try:
        state = _state(cache)
        token = (cache / "http-token").read_text(encoding="utf-8").strip()
        yield {**state, "cache": cache, "http_token": token, "proc": proc}
    finally:
        _stop(proc)


def _request(port: int, method: str, path: str, headers: dict[str, str] | None = None,
             body: bytes | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw


def _mcp_post(port: int, headers: dict[str, str]) -> int:
    return _request(port, "POST", "/mcp", {"Content-Type": "application/json",
                                           "Accept": "application/json, text/event-stream", **headers},
                    b"{}")[0]


async def _session(daemon: dict[str, Any], roots: list[Path], headers: dict[str, str] | None = None,
                   calls: Sequence[tuple[str, dict[str, Any]]] = ()) -> list[Any]:
    url = f"http://127.0.0.1:{daemon['http_port']}/mcp"
    transport = StreamableHttpTransport(url, headers={
        "Authorization": f"Bearer {daemon['http_token']}", **(headers or {}),
    })
    out = []
    async with Client(transport, roots=[r.as_uri() for r in roots]) as client:
        for name, args in calls:
            result = await client.call_tool(name, args, raise_on_error=False)
            out.append(result)
    return out


def _ids(result: Any) -> set[str]:
    assert not result.is_error, result
    return {n["id"] for n in result.structured_content["nodes"]}


ALL = ("kg_find_by_kind", {"kind": ""})
CRLF = "\r\n"
B_CRLF = CRLF.encode()


# --- per-session graph binding over HTTP -----------------------------------
def test_two_http_clients_see_only_their_own_graph(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    alpha, beta = _repo(tmp_path / "alpha", "alpha"), _repo(tmp_path / "beta", "beta")
    (alpha / "src" / "deep").mkdir()

    async def both() -> list[list[Any]]:
        return list(await asyncio.gather(
            _session(http_daemon, [alpha / "src" / "deep"], {"X-Codebase-KG-Client": "a"}, [ALL] * 10),
            _session(http_daemon, [beta], {"X-Codebase-KG-Client": "b"}, [ALL] * 10),
        ))

    a, b = asyncio.run(both())
    assert [_ids(r) for r in a] == [{"alpha_widget"}] * 10
    assert [_ids(r) for r in b] == [{"beta_widget"}] * 10


def test_the_graph_header_wins_over_the_roots(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    alpha, beta = _repo(tmp_path / "alpha", "alpha"), _repo(tmp_path / "beta", "beta")
    [result] = asyncio.run(_session(http_daemon, [beta], {
        "X-Codebase-KG-Client": "g", "X-Codebase-KG-Graph": str(_graph(alpha)),
    }, [ALL]))
    assert _ids(result) == {"alpha_widget"}


def test_a_relative_graph_header_resolves_against_the_roots(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    alpha = _repo(tmp_path / "alpha", "alpha")
    [result] = asyncio.run(_session(http_daemon, [alpha], {
        "X-Codebase-KG-Client": "rel", "X-Codebase-KG-Graph": "knowledge/code_graph.db",
    }, [ALL]))
    assert _ids(result) == {"alpha_widget"}


def test_the_cwd_header_needs_no_roots(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    alpha = _repo(tmp_path / "alpha", "alpha")
    [result] = asyncio.run(_session(http_daemon, [], {"X-Codebase-KG-Cwd": str(alpha)}, [ALL]))
    assert _ids(result) == {"alpha_widget"}


def test_a_client_without_a_graph_gets_the_stdio_error(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    [result] = asyncio.run(_session(http_daemon, [empty], {"X-Codebase-KG-Client": "e"}, [ALL]))
    assert result.is_error
    assert "No code_graph.db found" in result.content[0].text


def test_lookups_return_the_same_json_over_http_and_the_shim(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    alpha = _repo(tmp_path / "alpha", "alpha")
    calls = [
        ("kg_search", {"query": "widget"}), ("kg_node", {"id": "alpha_widget"}),
        ("kg_neighborhood", {"id": "alpha_widget"}), ALL,
        ("kg_find_by_path", {"path": "alpha.kt"}), ("kg_find_by_link", {"target": "x.db#y"}),
        ("kg_find_by_reference", {}),
    ]
    over_http = asyncio.run(_session(http_daemon, [alpha], {"X-Codebase-KG-Client": "p"}, calls))
    sock, f, reply = _raw(http_daemon, {
        "token": http_daemon["token"], "version": BUILD, "cwd": str(alpha), "graph_path": None,
    })
    assert reply["ok"] is True
    try:
        line = LineClient(f, f)
        line.initialize()
        over_shim = [line.call(name, **args) for name, args in calls]
    finally:
        sock.close()
    for (name, _), h, s in zip(calls, over_http, over_shim):
        assert not h.is_error and not s.get("isError"), name
        assert h.structured_content == s["structuredContent"], name


def test_a_server_without_workers_returns_what_the_pool_returns(
    http_daemon: dict[str, Any], tmp_path: Path,
) -> None:
    alpha = _repo(tmp_path / "alpha", "alpha")
    calls = [("kg_search", {"query": "widget"}), ("kg_node", {"id": "alpha_widget"}), ALL,
             ("kg_find_by_kind", {"kind": "component"}), ("kg_find_by_reference", {})]
    cache = tmp_path / "cache"
    proc = _start(cache, tmp_path, CODEBASE_KG_MAX_WORKERS="0")
    try:
        state = _state(cache)
        assert _request(state["http_port"], "GET", "/health")[1]["max_workers"] == 0
        token = (cache / "http-token").read_text(encoding="utf-8").strip()
        in_process = asyncio.run(_session({"http_port": state["http_port"], "http_token": token},
                                          [alpha], {"X-Codebase-KG-Client": "w0"}, calls))
    finally:
        _stop(proc)
    assert _request(http_daemon["http_port"], "GET", "/health")[1]["max_workers"] == 8
    pooled = asyncio.run(_session(http_daemon, [alpha], {"X-Codebase-KG-Client": "w4"}, calls))
    for (name, _), here, there in zip(calls, in_process, pooled):
        assert not here.is_error and not there.is_error, name
        assert here.structured_content == there.structured_content, name


def _roots(path: Path) -> dict[str, Any]:
    return {"inputResponses": {http_transport.ROOTS_REQUEST: {"roots": [{"uri": path.as_uri()}]}}}


def test_a_client_is_asked_for_its_roots_once(tmp_path: Path) -> None:
    clients = http_transport.Clients()
    headers = {"x-codebase-kg-client": "c1"}
    first = clients.connection(headers, {}, {"roots": {}})
    assert first == {"resultType": "input_required",
                     "inputRequests": {http_transport.ROOTS_REQUEST: {"method": "roots/list"}}}
    with pytest.raises(http_transport.RpcError) as refused:
        clients.connection(headers, {}, {"elicitation": {}})
    assert refused.value.code == -32021 and refused.value.data == {"requiredCapabilities": {"roots": {}}}
    bound = clients.connection(headers, _roots(tmp_path))
    assert getattr(bound, "cwd") == tmp_path  # noqa: B009 - pyright rejects `.cwd` on the dict arm of the union
    assert clients.connection(headers, {}) is bound


def test_a_root_that_is_not_a_local_file_is_refused(tmp_path: Path) -> None:
    answer = {"inputResponses": {http_transport.ROOTS_REQUEST: {"roots": [{"uri": "https://x/"}]}}}
    refused = http_transport.Clients().connection({"x-codebase-kg-client": "c"}, answer)
    assert isinstance(refused, dict) and refused["isError"] is True


def test_the_client_cache_is_bounded(tmp_path: Path) -> None:
    clients = http_transport.Clients(limit=2)
    for client in ("a", "b", "c"):
        clients.connection({"x-codebase-kg-client": client}, _roots(tmp_path))
    assert [k[0] for k in clients.bound] == ["b", "c"]


def _raw_http(port: int, payload: bytes, timeout: float = TIMEOUT) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(payload)
        out = b""
        while chunk := sock.recv(65536):
            out += chunk
        return out


def _list_request(port: int, token: str) -> tuple[bytes, bytes]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}}}}).encode()
    lines = [
        "POST /mcp HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Authorization: Bearer {token}",
        "Content-Type: application/json", "Accept: application/json, text/event-stream",
        "mcp-protocol-version: 2026-07-28", "mcp-method: tools/list", "Connection: close", "",
    ]
    return CRLF.join(lines).encode(), body


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}".encode() + B_CRLF + data + B_CRLF


def test_a_chunked_request_body_is_read(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    head, body = _list_request(port, http_daemon["http_token"])
    chunks = b"".join(_chunk(body[i:i + 7]) for i in range(0, len(body), 7))
    reply = _raw_http(port, head + b"Transfer-Encoding: chunked" + B_CRLF * 2 + chunks + b"0" + B_CRLF * 2)
    status, _, rest = reply.partition(B_CRLF)
    assert status == b"HTTP/1.1 200 OK"
    assert b'"name":"kg_search"' in rest


def test_a_request_body_over_the_cap_is_refused(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    head, _ = _list_request(port, http_daemon["http_token"])
    reply = _raw_http(port, head + f"Content-Length: {http_transport.MAX_BODY + 1}".encode() + B_CRLF * 2)
    assert reply.startswith(b"HTTP/1.1 413")
    head, _ = _list_request(port, http_daemon["http_token"])
    big = f"{http_transport.MAX_BODY + 1:x}".encode() + B_CRLF + b"x" * 1024
    reply = _raw_http(port, head + b"Transfer-Encoding: chunked" + B_CRLF * 2 + big)
    assert reply.startswith(b"HTTP/1.1 413")


def test_an_idle_keep_alive_connection_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_transport.Handler, "timeout", 0.5)
    sock = http_transport.bind(0)
    port = sock.getsockname()[1]
    server = http_transport.HttpServer(sock, core.Core(None), "t", "s", {"service": "codebase-kg"},
                                       on_request=lambda: None, on_shutdown=lambda: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
        try:
            conn.request("GET", "/health", headers={"Host": f"127.0.0.1:{port}"})
            response = conn.getresponse()
            assert response.status == 200 and response.read() == b'{"service":"codebase-kg"}'
            assert response.getheader("Connection") is None
            idle = time.monotonic()
            assert conn.sock is not None and conn.sock.recv(65536) == b""
        finally:
            conn.close()
        assert time.monotonic() - idle < 5.0
    finally:
        server.shutdown()
        server.server_close()


# --- security ---------------------------------------------------------------
def test_health_answers_without_a_token(http_daemon: dict[str, Any]) -> None:
    status, body = _request(http_daemon["http_port"], "GET", "/health")
    assert status == 200
    assert body["service"] == "codebase-kg" and body["build"] == BUILD
    assert body["pid"] == http_daemon["pid"]


def test_a_wrong_host_is_refused(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    auth = {"Authorization": f"Bearer {http_daemon['http_token']}"}
    assert _mcp_post(port, {**auth, "Host": f"evil.example:{port}"}) == 403
    assert _mcp_post(port, {**auth, "Host": "127.0.0.1:1"}) == 403
    assert _request(port, "GET", "/health", {"Host": "evil.example"})[0] == 403


def test_a_foreign_origin_is_refused(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    auth = {"Authorization": f"Bearer {http_daemon['http_token']}"}
    assert _mcp_post(port, {**auth, "Origin": "http://evil.example"}) == 403
    assert _mcp_post(port, {**auth, "Origin": f"http://localhost:{port}"}) != 403


def test_a_missing_or_wrong_token_is_refused(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    assert _mcp_post(port, {}) == 403
    assert _mcp_post(port, {"Authorization": "Bearer " + "0" * 64}) == 403
    assert _mcp_post(port, {"Authorization": f"Bearer {http_daemon['token']}"}) == 403


def test_shutdown_needs_the_servers_own_state_token(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start(cache, tmp_path)
    try:
        state = _state(cache)
        port = state["http_port"]
        http_token = (cache / "http-token").read_text(encoding="utf-8").strip()
        assert _request(port, "POST", "/shutdown", {"Authorization": f"Bearer {http_token}"})[0] == 403
        assert proc.poll() is None
        assert _request(port, "POST", "/shutdown", {"Authorization": f"Bearer {state['token']}"})[0] == 200
        proc.wait(timeout=TIMEOUT)
        assert not (cache / f"server-{BUILD}.json").exists()
    finally:
        _stop(proc)


def test_a_stopping_server_keeps_its_state_file_until_it_frees_the_port(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start(cache, tmp_path)
    try:
        state = _state(cache)
        port = state["http_port"]
        file = cache / f"server-{BUILD}.json"
        assert _request(port, "POST", "/shutdown", {"Authorization": f"Bearer {state['token']}"})[0] == 200
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline and shim.health(port, timeout=0.5)[0] == shim.OURS:
            assert file.exists(), "the port still answered after the state file was removed"
        proc.wait(timeout=TIMEOUT)
        assert not file.exists()
    finally:
        _stop(proc)


def test_an_http_server_outlives_the_shim_idle_timeout(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start(cache, tmp_path, CODEBASE_KG_IDLE_TIMEOUT="0.5")
    try:
        _state(cache)
        time.sleep(2.5)
        assert proc.poll() is None, "an HTTP server exited on the shim idle rule"
    finally:
        _stop(proc)


# --- the port: a fake holder ----------------------------------------------------
class _Holder:
    """A stand-in for whatever holds the port, recording each request it gets."""

    def __init__(self, health: dict[str, Any] | None, body: bytes | None = None) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        holder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                holder.requests.append((self.command, self.path, dict(self.headers)))
                payload = body if body is not None else json.dumps(health or {}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                if self.command == "POST" and self.path == "/shutdown":
                    threading.Thread(target=holder.close, daemon=True).start()

            do_GET = do_POST = _answer

            def log_message(self, format: str, *args: Any) -> None:
                pass

        http.server.ThreadingHTTPServer.allow_reuse_address = False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def authorizations(self) -> list[str]:
        return [h.get("Authorization", "") for _, _, h in self.requests if "Authorization" in h]


MINE = {"build": "9.9.9+mine", "version": "9.9.9", "built": 100.0, "released": True, "origin": "mine"}


def _older() -> dict[str, Any]:
    return {"service": "codebase-kg", "build": "0.0.1+0000000000aa", "version": "0.0.1", "built": 1.0,
            "pid": os.getpid()}


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache"


def test_a_free_port_is_bound(cache: Path) -> None:
    sock = http_transport.claim_port(_free_port(), MINE)
    assert sock is not None
    sock.close()


def test_another_program_keeps_the_port(cache: Path) -> None:
    holder = _Holder(None, body=b"<html>not us</html>")
    try:
        assert http_transport.claim_port(holder.port, MINE) is None
        assert holder.authorizations() == []
    finally:
        holder.close()


def test_an_unverified_older_build_receives_no_credential(cache: Path) -> None:
    holder = _Holder(_older())
    try:
        assert http_transport.claim_port(holder.port, MINE) is None
        assert [r[1] for r in holder.requests if r[0] == "POST"] == []
        assert holder.authorizations() == []
    finally:
        holder.close()


def test_a_verified_older_build_hands_over_the_port(cache: Path) -> None:
    health = _older()
    holder = _Holder(health)
    (cache / f"server-{health['build']}.json").write_text(json.dumps({
        "version": health["build"], "port": 1, "pid": os.getpid(), "token": "old-token",
        "http_port": holder.port,
    }), encoding="utf-8")
    sock = http_transport.claim_port(holder.port, MINE)
    try:
        assert sock is not None and sock.getsockname()[1] == holder.port
        assert holder.authorizations() == ["Bearer old-token"]
        assert [r[1] for r in holder.requests if r[0] == "POST"] == ["/shutdown"]
    finally:
        if sock is not None:
            sock.close()


def test_a_traversal_build_reads_no_file_outside_the_cache(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    planted = tmp_path / "elsewhere" / "server-x+0123456789ab.json"
    planted.parent.mkdir()
    build = "../elsewhere/x+0123456789ab"
    planted.write_text(json.dumps({"version": build, "port": 1, "pid": os.getpid(), "token": "t"}),
                       encoding="utf-8")
    holder = _Holder({**_older(), "build": build})
    reads: list[Path] = []
    real = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    try:
        assert shim.verified_holder(holder.port) is None
        assert http_transport.claim_port(holder.port, MINE) is None
        assert holder.authorizations() == []
    finally:
        holder.close()
    assert planted not in reads
    assert all(p.resolve().parent == cache.resolve() for p in reads if p.name.startswith("server-"))
    for bad in (build, r"..\x+0123456789ab", "0.1+short", "..+0123456789ab"):
        with pytest.raises(ValueError):
            shim.state_path(bad)


def test_a_state_file_for_another_build_is_rejected(cache: Path) -> None:
    health = _older()
    holder = _Holder(health)
    (cache / f"server-{health['build']}.json").write_text(json.dumps({
        "version": "0.0.2+0000000000cc", "port": 1, "pid": os.getpid(), "token": "old-token",
        "http_port": holder.port,
    }), encoding="utf-8")
    try:
        assert shim.read_state(health["build"]) is None
        assert shim.verified_holder(holder.port) is None
        assert http_transport.claim_port(holder.port, MINE) is None
        assert holder.authorizations() == []
    finally:
        holder.close()


def test_a_newer_build_keeps_the_port(cache: Path) -> None:
    newer = {**_older(), "build": "99.0.0+0000000000bb", "version": "99.0.0"}
    holder = _Holder(newer)
    try:
        assert http_transport.claim_port(holder.port, MINE) is None
        assert [r[1] for r in holder.requests if r[0] == "POST"] == []
    finally:
        holder.close()


def _build(build: str, version: str, released: bool = True, origin: str = "", built: float = 1.0) -> dict[str, Any]:
    return {"build": build, "version": version, "built": built, "released": released, "origin": origin or build}


def test_a_release_replaces_a_lower_version_and_nothing_else() -> None:
    old = _build("0.11.0+a", "0.11.0", built=50.0)
    new = _build("0.12.0+b", "0.12.0", built=10.0)
    assert shim.replaces(new, old, takeover=False) and not shim.replaces(old, new, takeover=False)
    assert not shim.replaces(new, dict(new), takeover=False)


def test_the_same_version_never_displaces_whatever_the_file_times() -> None:
    held = _build("0.12.0+b", "0.12.0", built=10.0)
    copy = _build("0.12.0+c", "0.12.0", built=99999.0)
    assert not shim.replaces(copy, held, takeover=False) and not shim.replaces(held, copy, takeover=False)


def test_a_copy_that_is_not_a_release_never_displaces_without_the_switch() -> None:
    held = _build("0.12.0+b", "0.12.0")
    checkout = _build("0.13.0+c", "0.13.0", released=False, built=99999.0)
    assert not shim.replaces(checkout, held, takeover=False)
    assert shim.replaces(checkout, held, takeover=True)
    assert not shim.replaces(checkout, dict(checkout), takeover=True)


def test_the_switch_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    held = _build("0.12.0+b", "0.12.0")
    checkout = _build("0.12.0+c", "0.12.0", released=False)
    monkeypatch.delenv("CODEBASE_KG_TAKEOVER", raising=False)
    assert not shim.replaces(checkout, held)
    monkeypatch.setenv("CODEBASE_KG_TAKEOVER", "1")
    assert shim.replaces(checkout, held)
    monkeypatch.setenv("CODEBASE_KG_TAKEOVER", "0")
    assert not shim.replaces(checkout, held)


def test_a_copy_replaces_the_server_that_runs_from_its_own_folder() -> None:
    before = _build("0.12.0+b", "0.12.0", released=False, origin="checkout")
    edited = _build("0.12.0+c", "0.12.0", released=False, origin="checkout")
    elsewhere = _build("0.12.0+d", "0.12.0", released=False, origin="scratch")
    assert shim.replaces(edited, before, takeover=False)
    assert not shim.replaces(elsewhere, before, takeover=False)


def test_a_holder_from_before_the_marker_is_replaced_by_a_higher_release_only() -> None:
    legacy = {"build": "0.17.1+a", "version": "0.17.1", "built": 9e9}
    assert shim.replaces(_build("0.18.0+b", "0.18.0"), legacy, takeover=False)
    assert not shim.replaces(_build("0.18.0+c", "0.18.0", released=False), legacy, takeover=False)
    assert not shim.replaces(_build("0.17.1+d", "0.17.1"), legacy, takeover=False)


def _tree(plugin: Path, marker: bool = False) -> Path:
    """A plugin folder with a server package in it, as a copy of the `main` tree unless `marker`."""
    package = plugin / "mcp" / "src" / "codebase_kg"
    package.mkdir(parents=True)
    (package / "shim.py").write_text("", encoding="utf-8")
    if marker:
        (package / shim.RELEASE_MARKER).write_text("9.9.9 test\n", encoding="utf-8")
    return package


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEBASE_KG_TAKEOVER"):
        monkeypatch.delenv(name, raising=False)
    return home


def _info(package: Path, version: str) -> dict[str, Any]:
    return {"build": f"{version}+{shim.origin(package)}", "version": version, "built": 1.0,
            "released": shim.released(package), "installed": shim.installed(package), "origin": shim.origin(package)}


HELD = {"build": "0.17.1+a", "version": "0.17.1", "built": 9e9}


@pytest.mark.parametrize("below_home", [
    ".claude/plugins/cache/alexk413x/codebase-kg/0.18.0-c0a92b89b510",  # Claude Code: marketplace/plugin/version
    ".codex/plugins/cache/alexk413x/codebase-kg/0.18.0",               # Codex: marketplace/plugin/version
    ".copilot/installed-plugins/alexk413x/codebase-kg",                # Copilot CLI: marketplace/plugin
    ".gemini/config/plugins/codebase-kg",                              # Antigravity CLI, global: plugin
])
def test_a_main_tree_copy_at_a_clients_plugin_depth_is_installed_and_replaces_a_lower_version(
    homes: Path, below_home: str,
) -> None:
    package = _tree(homes / below_home)
    assert shim.installed(package) and not shim.released(package)
    assert shim.replaces(_info(package, "0.18.0"), HELD)
    assert not shim.replaces(_info(package, "0.17.1"), HELD), "the same version never displaces"


@pytest.mark.parametrize("below_home", [
    "Projects/codebase-kg",                                             # a checkout
    "Projects/codebase-kg/.claude/worktrees/agy",                       # a worktree
    "scratchpad/evalcopy",                                              # the copy `claude plugin eval` runs
    ".claude/plugins/cache-old/alexk413x/codebase-kg/0.18.0",           # a name that only starts like the root
    ".claude/plugins/cache/alexk413x/codebase-kg",                      # one folder too high
    ".claude/plugins/cache/alexk413x/codebase-kg/0.18.0/evals/copy",    # a copy inside an installed plugin
    ".copilot/installed-plugins/codebase-kg",                           # Copilot CLI needs marketplace/plugin
    ".gemini/config/plugins/group/codebase-kg",                         # Antigravity CLI needs plugin alone
])
def test_the_same_tree_anywhere_else_is_a_development_copy_and_never_replaces(homes: Path, below_home: str) -> None:
    package = _tree(homes / below_home)
    assert not shim.installed(package)
    assert not shim.replaces(_info(package, "0.18.0"), HELD)


def test_a_workspace_copy_for_antigravity_counts_only_with_the_release_marker(homes: Path) -> None:
    """`<repo>/.agents/plugins/<plugin>` is a folder inside a repository, where any copy can sit."""
    plain = _tree(homes / "repo-a" / ".agents" / "plugins" / "codebase-kg")
    marked = _tree(homes / "repo-b" / ".agents" / "plugins" / "codebase-kg", marker=True)
    assert not shim.installed(plain) and not shim.installed(marked)
    assert not shim.replaces(_info(plain, "0.18.0"), HELD)
    assert shim.replaces(_info(marked, "0.18.0"), HELD)


def test_the_roots_follow_claude_config_dir_and_codex_home(homes: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude, codex = homes / "elsewhere" / "claude", homes / "elsewhere" / "codex"
    moved = [_tree(claude / "plugins" / "cache" / "m" / "codebase-kg" / "0.18.0"),
             _tree(codex / "plugins" / "cache" / "m" / "codebase-kg" / "0.18.0")]
    default = _tree(homes / ".claude" / "plugins" / "cache" / "m" / "codebase-kg" / "0.18.0")
    assert [shim.installed(p) for p in moved] == [False, False] and shim.installed(default)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    assert [shim.installed(p) for p in moved] == [True, True] and not shim.installed(default)


@pytest.mark.skipif(sys.platform != "win32", reason="the WMI start is the Windows path")
def test_the_server_started_outside_the_job_learns_where_the_plugin_folders_are(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WMI child inherits no environment. Without these two it would look under the default folders,
    find itself not installed and leave the port to the older server."""
    seen: dict[str, str] = {}
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/claude")
    monkeypatch.setenv("CODEX_HOME", "D:/codex")
    monkeypatch.setenv("UNRELATED_SETTING", "1")

    def run(command: list[str], **kwargs: Any) -> Any:
        seen["command"] = kwargs["env"]["KG_COMMAND"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    shim.spawn_server_outside_job(shim.server_build())
    assert "'CLAUDE_CONFIG_DIR': 'D:/claude'" in seen["command"] and "'CODEX_HOME': 'D:/codex'" in seen["command"]
    assert "UNRELATED_SETTING" not in seen["command"]


def test_a_cache_that_is_a_link_and_a_plugin_that_is_a_link_both_count(homes: Path) -> None:
    real_cache = homes / "disk" / "cache"
    package = _tree(real_cache / "m" / "codebase-kg" / "0.18.0")
    (homes / ".claude" / "plugins").mkdir(parents=True)
    try:
        (homes / ".claude" / "plugins" / "cache").symlink_to(real_cache, target_is_directory=True)
        elsewhere = _tree(homes / "disk" / "tree")
        linked = homes / ".codex" / "plugins" / "cache" / "m" / "codebase-kg"
        linked.mkdir(parents=True)
        (linked / "0.18.0").symlink_to(homes / "disk" / "tree", target_is_directory=True)
    except OSError:
        pytest.skip("this platform or account cannot make a directory link")
    assert shim.installed(package), "the copy's real path is under the link's target"
    assert shim.installed(homes / ".claude" / "plugins" / "cache" / "m" / "codebase-kg" / "0.18.0" / "mcp" / "src" / "codebase_kg")
    assert not shim.installed(elsewhere), "the link's target, named by its own path, is in no root"
    assert shim.installed(linked / "0.18.0" / "mcp" / "src" / "codebase_kg"), "named through the root, it is"


def test_this_checkout_is_not_installed() -> None:
    info = shim.build_info()
    assert info["installed"] is False and shim.installed() is False


def test_this_checkout_is_not_a_release() -> None:
    """Only the release workflow writes the marker. A marker on `main` or a feature branch would let every
    checkout, worktree and scratch copy of a bumped version take the shared port."""
    assert not (shim.PACKAGE_DIR / shim.RELEASE_MARKER).exists()
    info = shim.build_info()
    assert info["released"] is False and len(info["origin"]) == 12
    root = shim.MCP_DIR.parent
    marker = "mcp/src/codebase_kg/" + shim.RELEASE_MARKER
    assert marker in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    workflow = root / ".github" / "workflows" / "release.yml"
    if workflow.is_file():
        text = workflow.read_text(encoding="utf-8")
        assert f"> {marker}" in text and f"git add -f {marker}" in text
        assert f"git cat-file -e HEAD:{marker}" in text


def test_one_build_serves_two_ports_at_once(tmp_path: Path) -> None:
    """The state file was named by build alone, so a second instance on another port replaced it and the
    first one exited within ORPHAN_CHECK_INTERVAL."""
    from codebase_kg import daemon

    repo, cache = _repo(tmp_path / "repo", "repo"), tmp_path / "cache"
    ports = [_free_port(), _free_port()]
    procs = [_start(cache, repo, CODEBASE_KG_PORT=str(port)) for port in ports]

    def report() -> str:
        return (f"ports {ports}, health {[shim.health(p)[0] for p in ports]}, "
                f"exit codes {[proc.poll() for proc in procs]}, state files {sorted(f.name for f in cache.glob('server-*'))}")

    try:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline and not all(shim.health(p)[0] == shim.OURS for p in ports):
            time.sleep(0.1)
        assert all(shim.health(p)[0] == shim.OURS for p in ports), report()
        time.sleep(daemon.ORPHAN_CHECK_INTERVAL + 2.0)
        assert [proc.poll() for proc in procs] == [None, None], report()
        pids = [shim.health(port)[1].get("pid") for port in ports]
        assert pids[0] != pids[1] and all(pids)
        assert sorted(p.name for p in cache.glob(f"server-{BUILD}-p*.json")) == sorted(
            f"server-{BUILD}-p{port}.json" for port in ports)
    finally:
        for proc in procs:
            _stop(proc)


def test_server_start_gives_its_port_to_the_server_it_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--port` chose only the port to look at: the server took its port from the setting, which could be
    the shared one."""
    from codebase_kg import control

    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CODEBASE_KG_PORT", "47821")
    port, seen = _free_port(), []
    monkeypatch.setattr(control, "START_WAIT", 0.3)
    monkeypatch.setattr(shim, "spawn_server_detached", lambda build: seen.append(
        (os.environ.get("CODEBASE_KG_PORT"), shim.lock_path(build).name)))
    result = control.start(port)
    assert result["ok"] is False and seen == [(str(port), f"server-{BUILD}-p{port}.lock")]
    assert os.environ["CODEBASE_KG_PORT"] == "47821"


def test_the_state_file_is_named_by_build_and_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CODEBASE_KG_PORT", "47999")
    build = "0.18.0+0123456789ab"
    assert shim.state_path(build).name == f"server-{build}-p47999.json"
    assert shim.state_path(build, 48000) != shim.state_path(build)
    assert shim.lock_path(build, 48000).name == f"server-{build}-p48000.lock"
    # Builds before 0.18 read the name without a port, and they share the default port.
    assert shim.state_path(build, shim.DEFAULT_HTTP_PORT).name == f"server-{build}.json"
    assert shim.log_path(build).name == f"server-{build}.log"


def test_the_port_setting_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEBASE_KG_PORT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SERVER_PORT", raising=False)
    assert shim.http_port() == 47821
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SERVER_PORT", "47900")
    assert shim.http_port() == 47900
    monkeypatch.setenv("CODEBASE_KG_PORT", "47901")
    assert shim.http_port() == 47901


# --- the SessionStart hook ----------------------------------------------------
@pytest.fixture
def hook() -> Any:
    return _load(HOOK, "kg_server_start_under_test")


def _fake_shim(hook: Any, answers: list[tuple[str, dict[str, Any]]]) -> Any:
    real = hook.load_shim()
    spawned: list[str] = []
    calls = iter(answers)
    last = answers[-1] if answers else ("free", {})
    real.health = lambda port, timeout=1.0: next(calls, last)
    real.spawn_server_detached = lambda build: spawned.append(build)
    real.spawned = spawned
    return real


def test_the_hook_names_the_setting_when_another_program_holds_the_port(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [("other", {})])
    message = hook.ensure_server(fake)
    assert message is not None and "server_port" in message and "another program" in message
    assert fake.spawned == []


def test_the_hook_leaves_a_running_server_alone(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [])
    mine = fake.build_info()
    fake.health = lambda port, timeout=1.0: ("codebase-kg", {**mine, "service": "codebase-kg"})
    assert hook.ensure_server(fake) is None
    assert fake.spawned == []


def test_the_hook_starts_the_server_when_nothing_answers(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [])
    mine = fake.build_info()
    answers = iter([("free", {}), ("free", {})])
    fake.health = lambda port, timeout=1.0: next(answers, ("codebase-kg", {**mine, "service": "codebase-kg"}))
    assert hook.ensure_server(fake) is None
    assert fake.spawned == [mine["build"]]
    assert not shim.lock_path(mine["build"]).exists()


def test_the_hook_replaces_an_older_build(hook: Any, cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEBASE_KG_TAKEOVER", raising=False)
    fake = _fake_shim(hook, [])
    mine = {**fake.build_info(), "version": "9.9.9", "released": True}
    fake.build_info = lambda: mine
    answers = iter([("codebase-kg", _older())])
    fake.health = lambda port, timeout=1.0: next(answers, ("codebase-kg", {**mine, "service": "codebase-kg"}))
    assert hook.ensure_server(fake) is None
    assert fake.spawned == [mine["build"]]


def test_a_copy_that_is_not_a_release_leaves_the_port_and_says_so_in_one_line(
    hook: Any, cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEBASE_KG_TAKEOVER", raising=False)
    fake = _fake_shim(hook, [("codebase-kg", _older())])
    mine = {**fake.build_info(), "version": "9.9.9", "released": False}
    fake.build_info = lambda: mine
    message = hook.ensure_server(fake)
    assert fake.spawned == []
    assert message is not None and "\n" not in message
    assert "version 0.0.1" in message and "server_port" in message and "development copy" in message


def test_an_installed_copy_from_main_gets_no_development_notice(
    hook: Any, cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marketplace install can hold the `main` tree: no marker, yet a client installed it."""
    monkeypatch.delenv("CODEBASE_KG_TAKEOVER", raising=False)
    fake = _fake_shim(hook, [("codebase-kg", {**_older(), "version": "9.9.9"})])
    mine = {**fake.build_info(), "version": "9.9.9", "released": False, "installed": True}
    fake.build_info = lambda: mine
    assert hook.ensure_server(fake) is None and fake.spawned == []


def test_the_switch_lets_a_copy_that_is_not_a_release_replace_the_holder(
    hook: Any, cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEBASE_KG_TAKEOVER", "1")
    fake = _fake_shim(hook, [])
    mine = {**fake.build_info(), "released": False}
    fake.build_info = lambda: mine
    answers = iter([("codebase-kg", _older())])
    fake.health = lambda port, timeout=1.0: next(answers, ("codebase-kg", {**mine, "service": "codebase-kg"}))
    assert hook.ensure_server(fake) is None
    assert fake.spawned == [mine["build"]]


def test_a_release_that_finds_a_higher_or_equal_version_says_nothing(
    hook: Any, cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEBASE_KG_TAKEOVER", raising=False)
    fake = _fake_shim(hook, [("codebase-kg", _older())])
    mine = {**fake.build_info(), "version": "0.0.1", "released": True}
    fake.build_info = lambda: mine
    assert hook.ensure_server(fake) is None and fake.spawned == []


def test_the_hook_prints_one_line_when_the_server_does_not_come_up(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [("free", {})])
    message = hook.ensure_server(fake, wait=0.3)
    assert message is not None and "\n" not in message and "did not answer" in message


def test_the_hook_runs_silently_and_exits_zero_when_a_server_answers(http_daemon: dict[str, Any]) -> None:
    env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(http_daemon["cache"]),
           "CODEBASE_KG_PORT": str(http_daemon["http_port"])}
    proc = subprocess.run([sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
                          env=env, timeout=TIMEOUT, check=False)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_the_hook_is_registered_for_session_start() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [h["command"] for entry in hooks["SessionStart"] for h in entry["hooks"]]
    assert f'{LAUNCHER}kg_server_start.cmd"; exit $LASTEXITCODE' in commands


# --- the headers helper ------------------------------------------------------
def _helper(env: dict[str, str]) -> dict[str, str]:
    full = {k: v for k, v in os.environ.items() if k != "CODEBASE_KG_PATH"}
    proc = subprocess.run([sys.executable, str(HEADERS)], capture_output=True, text=True,
                          env={**full, **env}, timeout=TIMEOUT, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_helper_sends_the_token_only_to_a_verified_server(http_daemon: dict[str, Any]) -> None:
    port = http_daemon["http_port"]
    out = _helper({"CODEBASE_KG_CACHE_DIR": str(http_daemon["cache"]),
                   "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp",
                   "CODEBASE_KG_PATH": "knowledge/my graph.db"})
    assert out["Authorization"] == f"Bearer {http_daemon['http_token']}"
    assert len(out["X-Codebase-KG-Client"]) == 32
    assert out["X-Codebase-KG-Graph"] == "knowledge/my%20graph.db"


def test_the_helper_sends_no_token_to_a_squatter(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "http-token").write_text("secret", encoding="utf-8")
    holder = _Holder(_older())
    try:
        out = _helper({"CODEBASE_KG_CACHE_DIR": str(cache),
                       "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{holder.port}/mcp"})
    finally:
        holder.close()
    assert "Authorization" not in out
    assert holder.authorizations() == []


def test_the_helper_prints_json_with_no_server(tmp_path: Path) -> None:
    started = time.monotonic()
    out = _helper({"CODEBASE_KG_CACHE_DIR": str(tmp_path),
                   "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{_free_port()}/mcp"})
    assert set(out) == {"X-Codebase-KG-Client", "X-Codebase-KG-Agent"}
    assert time.monotonic() - started < helper_module().WAIT + 5


def helper_module() -> Any:
    return _load(HEADERS, "kg_headers_under_test")


def test_the_helper_waits_for_a_server_that_is_starting(tmp_path: Path) -> None:
    """A request without the token gets 403, which Claude Code records as needing auth."""
    cache = tmp_path / "cache"
    port = _free_port()
    full = {k: v for k, v in os.environ.items() if k != "CODEBASE_KG_PATH"}
    helper = subprocess.Popen(
        [sys.executable, str(HEADERS)], stdout=subprocess.PIPE, text=True,
        env={**full, "CODEBASE_KG_CACHE_DIR": str(cache), "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp"},
    )
    time.sleep(0.5)
    proc = _start(cache, tmp_path, CODEBASE_KG_PORT=str(port))
    try:
        out, _ = helper.communicate(timeout=TIMEOUT)
        token = (cache / "http-token").read_text(encoding="utf-8").strip()
        assert json.loads(out)["Authorization"] == f"Bearer {token}"
    finally:
        _stop(proc)


def test_the_helper_keeps_polling_while_a_codebase_kg_does_not_verify() -> None:
    """A server handing the port over answers /health after its state file is gone."""
    fake = _load(SRC_SHIM, "shim_for_unverified")
    fake.health = lambda port, timeout=1.0: (fake.OURS, {})
    tokens = iter([None, None, "tok"])
    fake.verified_token = lambda port: next(tokens, None)
    assert helper_module().token(fake, 1, wait=5.0) == "tok"


def test_the_helper_gives_up_on_a_codebase_kg_that_never_verifies() -> None:
    fake = _load(SRC_SHIM, "shim_for_never_verified")
    fake.health = lambda port, timeout=1.0: (fake.OURS, {})
    fake.verified_token = lambda port: None
    started = time.monotonic()
    assert helper_module().token(fake, 1, wait=0.5) is None
    assert time.monotonic() - started < 3.0


def test_the_helper_does_not_wait_on_another_program(tmp_path: Path) -> None:
    holder = _Holder(None, body=b"<html>not us</html>")
    try:
        started = time.monotonic()
        assert helper_module().token(_load(SRC_SHIM, "shim_for_helper"), holder.port, wait=10.0) is None
        assert time.monotonic() - started < 3.0
    finally:
        holder.close()


# --- the helper as Codex runs it: arguments, no environment -------------------
def _codex_helper(args: list[str], cwd: Path, **extra: str) -> dict[str, str]:
    """Run the helper the way Codex does: the base interpreter with -I -S, a bare environment, the session's folder."""
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME") if k in os.environ}
    proc = subprocess.run([sys.executable, "-I", "-S", str(HEADERS), *args], capture_output=True, text=True,
                          cwd=cwd, env={**env, **extra}, timeout=TIMEOUT, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_helper_takes_every_setting_as_an_argument(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    out = _codex_helper(["--port", str(http_daemon["http_port"]), "--home", str(http_daemon["cache"]), "--cwd",
                         "--graph", "knowledge/my graph.db"], tmp_path)
    assert out["Authorization"] == f"Bearer {http_daemon['http_token']}"
    assert unquote(out["X-Codebase-KG-Cwd"]) == str(tmp_path)
    assert out["X-Codebase-KG-Graph"] == "knowledge/my%20graph.db"


def test_an_argument_beats_the_environment(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    out = _codex_helper(
        ["--port", str(http_daemon["http_port"]), "--home", str(http_daemon["cache"]), "--graph", "from-arg.db"],
        tmp_path, CLAUDE_CODE_MCP_SERVER_URL="http://127.0.0.1:1/mcp", CODEBASE_KG_PATH="from-env.db",
        CODEBASE_KG_PORT="1", CODEBASE_KG_CACHE_DIR=str(tmp_path / "elsewhere"),
    )
    assert out["Authorization"] == f"Bearer {http_daemon['http_token']}"
    assert out["X-Codebase-KG-Graph"] == "from-arg.db"


def test_the_helper_sends_the_cwd_only_when_asked(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    out = _codex_helper(["--port", str(http_daemon["http_port"]), "--home", str(http_daemon["cache"])], tmp_path)
    assert "X-Codebase-KG-Cwd" not in out and "Authorization" in out


def test_the_helper_ignores_a_word_it_does_not_know(http_daemon: dict[str, Any], tmp_path: Path) -> None:
    out = _codex_helper(["--bogus", "--port", str(http_daemon["http_port"]), "--home"], tmp_path)
    assert "X-Codebase-KG-Client" in out


def test_the_helper_starts_the_server_when_asked(tmp_path: Path) -> None:
    cache, port = tmp_path / "cache", _free_port()
    try:
        out = _codex_helper(["--port", str(port), "--home", str(cache), "--cwd", "--start"], tmp_path)
        assert out["Authorization"] == f"Bearer {(cache / 'http-token').read_text(encoding='utf-8').strip()}"
        assert shim.health(port)[0] == shim.OURS
        assert list((cache / "sessions").glob("codex-*.json"))
    finally:
        env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache)}
        subprocess.run([sys.executable, "-I", str(CLI), "server", "stop", "--port", str(port)], env=env,
                       capture_output=True, timeout=TIMEOUT, check=False)


def test_the_helper_starts_no_server_without_start(tmp_path: Path) -> None:
    port = _free_port()
    out = _codex_helper(["--port", str(port), "--home", str(tmp_path / "cache")], tmp_path)
    assert "Authorization" not in out
    assert shim.health(port)[0] == shim.FREE


def test_the_server_start_hook_takes_a_spawner(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [])
    mine = fake.build_info()
    answers = iter([("free", {}), ("free", {})])
    fake.health = lambda port, timeout=1.0: next(answers, ("codebase-kg", {**mine, "service": "codebase-kg"}))
    spawned: list[str] = []
    assert hook.ensure_server(fake, spawn=spawned.append) is None
    assert spawned == [mine["build"]] and fake.spawned == []


def test_the_outside_job_spawner_runs_the_server_through_wmi(
    cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("CODEBASE_KG_PORT", "47999")
    monkeypatch.setenv("CODEBASE_KG_PATH", "must-not-reach-the-server.db")
    shim.spawn_server_outside_job(BUILD)
    (command, kwargs), = calls
    assert Path(command[0]).name == "powershell.exe" and "Win32_Process" in command[-1]
    line = kwargs["env"]["KG_COMMAND"]
    assert "-I -S -c" in line and line.endswith("--serve") and "'CODEBASE_KG_PORT': '47999'" in line
    assert "must-not-reach-the-server" not in line
    assert repr(str(shim.log_path(BUILD)))[1:-1] in line
    assert kwargs["env"]["KG_CWD"] == str(cache)
    assert "ProcessStartupInformation=$si" in command[-1] and "ShowWindow=[uint16]0" in command[-1]


def test_the_outside_job_spawner_forwards_the_plugin_settings(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SERVER_PORT", "47998")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MAX_WORKERS", "3")
    shim.spawn_server_outside_job(BUILD)
    line = calls[0]["env"]["KG_COMMAND"]
    assert "'CLAUDE_PLUGIN_OPTION_SERVER_PORT': '47998'" in line
    assert "'CLAUDE_PLUGIN_OPTION_MAX_WORKERS': '3'" in line


def test_the_outside_job_spawner_reports_a_wmi_failure(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 2))
    with pytest.raises(OSError, match="returned 2"):
        shim.spawn_server_outside_job(BUILD)


def _terminal_pids() -> set[str]:
    listing = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process WindowsTerminal,OpenConsole -ErrorAction SilentlyContinue | ForEach-Object Id) -join ','"],
        capture_output=True, text=True, timeout=TIMEOUT, check=False, creationflags=shim.NO_WINDOW,
    ).stdout
    return set(listing.strip().split(",")) - {""}


@pytest.mark.skipif(sys.platform != "win32", reason="WMI exists only on Windows")
def test_the_outside_job_spawner_starts_a_server_that_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, port = tmp_path / "cache", _free_port()
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(cache))
    monkeypatch.setenv("CODEBASE_KG_PORT", str(port))
    cache.mkdir()
    terminals = _terminal_pids()
    try:
        shim.spawn_server_outside_job(BUILD)
        deadline = time.monotonic() + TIMEOUT
        while shim.health(port)[0] != shim.OURS and time.monotonic() < deadline:
            time.sleep(0.1)
        assert shim.health(port)[0] == shim.OURS
        assert shim.verified_holder(port) is not None
        assert _terminal_pids() <= terminals, "the server start opened a terminal window"
    finally:
        subprocess.run([sys.executable, "-I", str(CLI), "server", "stop", "--port", str(port)],
                       env={**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache)},
                       capture_output=True, timeout=TIMEOUT, check=False)


# --- kg_cli.py server status|stop ----------------------------------------------
CLI = ROOT / "mcp" / "launch" / "kg_cli.py"


def _cli(cache: Path, *args: str) -> dict[str, Any]:
    env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache)}
    proc = subprocess.run([sys.executable, "-I", str(CLI), "server", *args], capture_output=True, text=True,
                          env=env, timeout=TIMEOUT, check=False)
    return json.loads(proc.stdout)


def test_the_cli_reports_and_stops_the_server_on_the_port(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start(cache, tmp_path, CODEBASE_KG_MAX_WORKERS="3")
    try:
        port = str(_state(cache)["http_port"])
        assert _cli(cache, "status", "--port", port) == {
            "ok": True, "port": int(port), "running": True, "build": BUILD, "pid": proc.pid, "max_workers": 3,
        }
        assert _cli(cache, "stop", "--port", port) == {"ok": True, "port": int(port), "running": False,
                                                       "stopped": proc.pid}
        assert proc.wait(timeout=TIMEOUT) == 0
        assert _cli(cache, "status", "--port", port)["running"] is False
    finally:
        _stop(proc)


def test_the_cli_sends_no_credential_to_a_squatter(cache: Path) -> None:
    from codebase_kg import control

    holder = _Holder(_older())
    try:
        out = control.stop(holder.port)
    finally:
        holder.close()
    assert out["ok"] is False and "matches no state file" in str(out["error"])
    assert holder.authorizations() == []


def test_a_closed_client_connection_reads_as_gone_and_an_open_one_does_not() -> None:
    """Closing the connection is how a 2026-07-28 client cancels a request over HTTP."""
    ours, theirs = socket.socketpair()
    try:
        handler = http_transport.Handler.__new__(http_transport.Handler)
        handler.connection = ours
        assert handler._client_gone() is False
        theirs.sendall(b"POST /mcp HTTP/1.1")
        assert handler._client_gone() is False, "a client that sent its next request has not gone"
        ours.recv(64)
        theirs.close()
        deadline = time.monotonic() + 5
        while not handler._client_gone() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert handler._client_gone() is True
    finally:
        ours.close()


def test_clear_dead_states_keeps_a_running_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path))
    ended = subprocess.Popen([sys.executable, "-c", ""])
    ended.wait()
    dead = tmp_path / "server-0.1.0+aaaaaaaaaaaa-p5.json"
    dead.write_text(json.dumps({"pid": ended.pid}), encoding="utf-8")
    alive = tmp_path / "server-0.1.0+bbbbbbbbbbbb.json"
    alive.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    unread = tmp_path / "server-0.1.0+cccccccccccc.json"
    unread.write_text("not json", encoding="utf-8")
    (tmp_path / "http-token").write_text("t", encoding="utf-8")

    shim.clear_dead_states()

    assert sorted(p.name for p in tmp_path.iterdir()) == ["http-token", alive.name, unread.name]


def test_a_starting_server_removes_the_state_of_a_dead_one(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    ended = subprocess.Popen([sys.executable, "-c", ""])
    ended.wait()
    dead = cache / "server-0.1.0+aaaaaaaaaaaa-p5.json"
    dead.write_text(json.dumps({"pid": ended.pid}), encoding="utf-8")
    proc = _start(cache, tmp_path)
    try:
        _state(cache)
        deadline = time.monotonic() + TIMEOUT
        while dead.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not dead.exists()
    finally:
        proc.kill()
        proc.wait()

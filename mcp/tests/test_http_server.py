"""The shared server over Streamable HTTP, its port handover, the start hook and the headers helper.

Claude Code speaks MCP 2026-07-28 over HTTP: no sessions, and a headersHelper
that cannot see the session's cwd. The server learns each client's cwd from its
roots, asked for once through an `InputRequiredResult`, and must never cross
two clients' graphs. These tests drive it with fastmcp's client, which answers
that request the way Claude Code does.
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Sequence

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.datastructures import Headers

from codebase_kg import http_transport, shim
from test_shared_server import BUILD, SRC, TIMEOUT, _graph, _raw, _repo, _stop
from test_shared_server import Client as LineClient

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "kg_server_start.py"
HEADERS = ROOT / "mcp" / "launch" / "kg_headers.py"


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
    full["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), full.get("PYTHONPATH")]))
    return subprocess.Popen(
        [sys.executable, "-m", "codebase_kg.server", "--serve"],
        cwd=cwd, env=full, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _state(cache: Path) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    path = cache / f"server-{BUILD}.json"
    while time.monotonic() < deadline:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
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


def _ctx(protocol: str = http_transport.MODERN_PROTOCOL, params: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(protocol_version=protocol, params=params or {})


def test_a_client_is_asked_for_its_roots_once(tmp_path: Path) -> None:
    binding = http_transport.SessionBinding()
    headers = Headers({"x-codebase-kg-client": "c1"})
    first = binding.connection(_ctx(), headers)
    assert first.__class__.__name__ == "InputRequiredResult"
    answer = {http_transport.ROOTS_REQUEST: {"roots": [{"uri": tmp_path.as_uri()}]}}
    bound = binding.connection(_ctx(params={"inputResponses": answer}), headers)
    assert getattr(bound, "cwd") == tmp_path
    assert binding.connection(_ctx(), headers) is bound


def test_a_legacy_client_without_a_cwd_gets_an_error() -> None:
    result = http_transport.SessionBinding().connection(_ctx("2025-06-18"), Headers({}))
    assert getattr(result, "is_error") is True


def test_the_client_cache_is_bounded(tmp_path: Path) -> None:
    binding = http_transport.SessionBinding(limit=2)
    answer = {"inputResponses": {http_transport.ROOTS_REQUEST: {"roots": [{"uri": tmp_path.as_uri()}]}}}
    for client in ("a", "b", "c"):
        binding.connection(_ctx(params=answer), Headers({"x-codebase-kg-client": client}))
    assert [k[0] for k in binding.clients] == ["b", "c"]


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


MINE = {"build": "9.9.9+mine", "version": "9.9.9", "built": 100.0}


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


def test_builds_order_by_version_then_source_time() -> None:
    old = {"build": "0.11.0+a", "version": "0.11.0", "built": 50.0}
    new = {"build": "0.12.0+b", "version": "0.12.0", "built": 10.0}
    edited = {"build": "0.12.0+c", "version": "0.12.0", "built": 20.0}
    assert shim.outranks(new, old) and not shim.outranks(old, new)
    assert shim.outranks(edited, new) and not shim.outranks(new, edited)
    assert not shim.outranks(new, dict(new))


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
    real = hook._shim()
    spawned: list[str] = []
    calls = iter(answers)
    last = answers[-1] if answers else ("free", {})
    real.health = lambda port, timeout=1.0: next(calls, last)
    real.spawn_server = lambda build: spawned.append(build)
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


def test_the_hook_replaces_an_older_build(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [])
    mine = fake.build_info()
    answers = iter([("codebase-kg", _older())])
    fake.health = lambda port, timeout=1.0: next(answers, ("codebase-kg", {**mine, "service": "codebase-kg"}))
    assert hook.ensure_server(fake) is None
    assert fake.spawned == [mine["build"]]


def test_the_hook_prints_one_line_when_the_server_does_not_come_up(hook: Any, cache: Path) -> None:
    fake = _fake_shim(hook, [("free", {})])
    message = hook.ensure_server(fake, wait=0.3)
    assert message is not None and "\n" not in message and "did not answer" in message


def test_the_hook_runs_silently_and_exits_zero_when_a_server_answers(http_daemon: dict[str, Any]) -> None:
    env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(http_daemon["cache"]),
           "CODEBASE_KG_PORT": str(http_daemon["http_port"])}
    proc = subprocess.run([sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
                          env=env, timeout=TIMEOUT)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_the_hook_is_registered_for_session_start() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [h["command"] for entry in hooks["SessionStart"] for h in entry["hooks"]]
    script = '"${CLAUDE_PLUGIN_ROOT}/hooks/kg_server_start.py"'
    assert f"command -v py >/dev/null 2>&1 && py -3 {script} || python3 {script} || python {script}" in commands


# --- the headers helper ------------------------------------------------------
def _helper(env: dict[str, str]) -> dict[str, str]:
    full = {k: v for k, v in os.environ.items() if k != "CODEBASE_KG_PATH"}
    proc = subprocess.run([sys.executable, str(HEADERS)], capture_output=True, text=True,
                          env={**full, **env}, timeout=TIMEOUT)
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
    out = _helper({"CODEBASE_KG_CACHE_DIR": str(tmp_path),
                   "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{_free_port()}/mcp"})
    assert set(out) == {"X-Codebase-KG-Client"}

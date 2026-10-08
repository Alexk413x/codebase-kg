"""Shared-server mode: one `--serve` process, many sessions, no crossed graphs.

The server that used to run once per session now runs once per machine, so its
own cwd, environment and argv belong to whichever session started it. The
contamination tests start it from inside a repo that has a graph, with
`CODEBASE_KG_PATH` pointing at that graph, and prove no connection ever sees it.

The end-to-end tests launch `shim.py` the way a stdio client does.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest
from codebase_kg import pool, resolve, shim
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

SRC = Path(__file__).resolve().parent.parent / "src"
SHIM = SRC / "codebase_kg" / "shim.py"
BUILD = shim.server_build()
TIMEOUT = 30.0

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _repo(root: Path, name: str) -> Path:
    """A repo whose graph holds one node, `<name>_widget`, and nothing else."""
    src = root / "src"
    src.mkdir(parents=True)
    (src / f"{name}.kt").write_text(f"class {name.title()}\n", encoding="utf-8")
    build(
        root / "knowledge" / "code_graph.db",
        Meta(codebase=name, root="src", generated="2026-09-25",
             covers=["**/*.kt"], exempt=["**/*.kt"]),
        [Node(id=f"{name}_widget", kind="Widget", description=f"The {name} widget.",
              anchors=[Anchor(f"{name}.kt", name.title())])],
        source_root=src,
    )
    return root


def _graph(repo: Path) -> Path:
    return repo / "knowledge" / "code_graph.db"


class Client:
    """Newline-delimited JSON-RPC over any pair of byte streams, with timeouts."""

    def __init__(self, write: IO[bytes], read: IO[bytes]) -> None:
        self._write = write
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._next_id = 0
        self.seen: list[dict[str, Any]] = []
        threading.Thread(target=self._pump, args=(read,), daemon=True).start()

    def _pump(self, read: IO[bytes]) -> None:
        try:
            for line in iter(read.readline, b""):
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(b"")

    def send(self, obj: dict[str, Any]) -> None:
        self._write.write(json.dumps(obj).encode("utf-8") + b"\n")
        self._write.flush()

    def recv(self) -> dict[str, Any]:
        line = self._lines.get(timeout=TIMEOUT)
        assert line, "stream closed"
        self.seen.append(json.loads(line))
        return self.seen[-1]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        while True:
            msg = self.recv()
            if msg.get("id") == self._next_id:
                return msg

    def initialize(self) -> dict[str, Any]:
        out = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return out

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def data(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = self.call(name, **arguments)
        assert not result.get("isError"), result
        return result["structuredContent"]


def _error_text(result: dict[str, Any]) -> str:
    assert result.get("isError") is True, result
    return " ".join(c.get("text", "") for c in result["content"])


def _node_ids(client: Client) -> set[str]:
    return {n["id"] for n in client.data("kg_find_by_kind", kind="")["nodes"]}


# --- a daemon started from inside a repo it must never serve ------------------
def _start_daemon(cache: Path, cwd: Path, env_extra: dict[str, str]) -> subprocess.Popen[bytes]:
    env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache), **env_extra}
    return subprocess.Popen(
        shim.server_command("--serve"),
        cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_for_state(cache: Path) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    state_file = cache / f"server-{BUILD}.json"
    while time.monotonic() < deadline:
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"no state file at {state_file}")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=TIMEOUT)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("shared")
    decoy = _repo(root / "decoy", "decoy")
    cache = root / "cache"
    proc = _start_daemon(cache, decoy, {
        "CODEBASE_KG_PATH": str(_graph(decoy)), "CODEBASE_KG_IDLE_TIMEOUT": "120",
    })
    try:
        state = _wait_for_state(cache)
        yield {**state, "root": root, "cache": cache}
    finally:
        _stop(proc)


def _raw(state: dict[str, Any], hello: dict[str, Any]) -> tuple[socket.socket, Any, dict[str, Any]]:
    sock = socket.create_connection(("127.0.0.1", state["port"]), timeout=TIMEOUT)
    f = sock.makefile("rwb")
    f.write(json.dumps(hello).encode("utf-8") + b"\n")
    f.flush()
    reply = json.loads(f.readline())
    sock.settimeout(None)
    return sock, f, reply


def _hang_up(sock: socket.socket) -> None:
    """Close for real: `close()` alone waits on the makefile() wrapper still open."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


@pytest.fixture
def connect(daemon: dict[str, Any]) -> Iterator[Any]:
    opened: list[socket.socket] = []

    def _connect(cwd: Path, graph_path: Path | None = None) -> Client:
        sock, f, reply = _raw(daemon, {
            "token": daemon["token"], "version": BUILD, "cwd": str(cwd),
            "graph_path": str(graph_path) if graph_path else None,
        })
        assert reply["ok"] is True, reply
        opened.append(sock)
        client = Client(f, f)
        client.initialize()
        return client

    yield _connect
    for sock in opened:
        _hang_up(sock)


@pytest.fixture
def two_repos(tmp_path: Path) -> tuple[Path, Path]:
    return _repo(tmp_path / "alpha", "alpha"), _repo(tmp_path / "beta", "beta")


def test_two_connections_see_only_their_own_graph(connect: Any, two_repos: tuple[Path, Path]) -> None:
    alpha, beta = two_repos
    (alpha / "src" / "deep").mkdir()
    a = connect(alpha / "src" / "deep")
    b = connect(beta)
    results: dict[str, list[set[str]]] = {"a": [], "b": []}

    def hammer(name: str, client: Client) -> None:
        for _ in range(15):
            results[name].append(_node_ids(client))

    threads = [threading.Thread(target=hammer, args=("a", a)),
               threading.Thread(target=hammer, args=("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert results["a"] == [{"alpha_widget"}] * 15
    assert results["b"] == [{"beta_widget"}] * 15
    assert a.data("kg_node", id="beta_widget")["found"] is False
    assert b.data("kg_node", id="alpha_widget")["found"] is False


def test_a_write_changes_only_the_writing_connections_graph(
    connect: Any, two_repos: tuple[Path, Path]
) -> None:
    alpha, beta = two_repos
    a, b = connect(alpha), connect(beta)
    before_a, before_b = _sha(_graph(alpha)), _sha(_graph(beta))
    out = a.data("kg_upsert_node", nodes=[{"id": "alpha_widget", "description": "Rewritten."}])
    assert out["ok"] is True and out["written"] is True, out
    assert _sha(_graph(alpha)) != before_a
    assert _sha(_graph(beta)) == before_b
    assert b.data("kg_node", id="beta_widget")["description"] == "The beta widget."
    g = CodeGraph(_graph(alpha))
    try:
        node = g.node("alpha_widget")
        assert node is not None and node.description == "Rewritten."
    finally:
        g.close()


def test_a_connection_without_a_graph_gets_the_stdio_error(
    connect: Any, two_repos: tuple[Path, Path], tmp_path: Path,
) -> None:
    alpha, _ = two_repos
    empty = tmp_path / "empty"
    empty.mkdir()
    expected = resolve.missing_graph_message(resolve.Connection(cwd=empty))

    a = connect(alpha)
    assert _node_ids(a) == {"alpha_widget"}
    lost = connect(empty)
    assert expected in _error_text(lost.call("kg_find_by_kind", kind="x"))
    assert expected in _error_text(lost.call("kg_search", query="widget"))
    assert _node_ids(a) == {"alpha_widget"}


def test_the_handshake_graph_path_wins_over_the_cwd(
    connect: Any, two_repos: tuple[Path, Path]
) -> None:
    alpha, beta = two_repos
    client = connect(beta, graph_path=_graph(alpha))
    assert _node_ids(client) == {"alpha_widget"}


def test_the_local_override_is_read_from_the_connections_cwd(
    connect: Any, tmp_path: Path
) -> None:
    other = _repo(tmp_path / "other", "other")
    repo = tmp_path / "custom"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "codebase-kg.local.md").write_text(
        f"---\ngraph_path: {_graph(other).as_posix()}\n---\n", encoding="utf-8"
    )
    (repo / "sub").mkdir()
    assert _node_ids(connect(repo / "sub")) == {"other_widget"}


def test_a_bad_token_is_rejected(daemon: dict[str, Any], tmp_path: Path) -> None:
    sock, f, reply = _raw(daemon, {
        "token": "0" * 64, "version": BUILD, "cwd": str(tmp_path), "graph_path": None,
    })
    try:
        assert reply == {"ok": False, "error": "bad token"}
        assert f.readline() == b""
    finally:
        _hang_up(sock)


def test_a_version_mismatch_is_rejected(daemon: dict[str, Any], tmp_path: Path) -> None:
    sock, f, reply = _raw(daemon, {
        "token": daemon["token"], "version": "0.0.0", "cwd": str(tmp_path), "graph_path": None,
    })
    try:
        assert reply["ok"] is False and "version mismatch" in reply["error"]
        assert f.readline() == b""
    finally:
        _hang_up(sock)


def test_the_shim_refuses_a_rejected_server(daemon: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(shim.Rejected, match="version mismatch"):
        shim.handshake(daemon, {"version": "0.0.0", "cwd": str(tmp_path), "graph_path": None})


# --- idle exit ----------------------------------------------------------------
def test_idle_exit_removes_the_state_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start_daemon(cache, tmp_path, {
        "CODEBASE_KG_IDLE_TIMEOUT": "1.5", "CODEBASE_KG_HTTP_IDLE_TIMEOUT": "1.5",
    })
    try:
        state = _wait_for_state(cache)
        repo = _repo(tmp_path / "alpha", "alpha")
        sock, f, reply = _raw(state, {
            "token": state["token"], "version": BUILD, "cwd": str(repo), "graph_path": None,
        })
        assert reply["ok"] is True
        client = Client(f, f)
        client.initialize()
        time.sleep(3)
        assert proc.poll() is None, "exited with a connection still open"
        assert _node_ids(client) == {"alpha_widget"}
        _hang_up(sock)
        proc.wait(timeout=TIMEOUT)
        assert not (cache / f"server-{BUILD}.json").exists()
    finally:
        _stop(proc)


# --- the shim, launched as a stdio client launches it ------------------------------
def _shim(cwd: Path, cache: Path, **env: str) -> subprocess.Popen[bytes]:
    full = {k: v for k, v in os.environ.items()
            if k not in {"CODEBASE_KG_PATH", "CODEBASE_KG_SHARED", "VIRTUAL_ENV"}}
    full.update({"CODEBASE_KG_CACHE_DIR": str(cache), "CODEBASE_KG_IDLE_TIMEOUT": "60", **env})
    return subprocess.Popen(
        [shutil.which("python3") or sys.executable, str(SHIM)], cwd=cwd, env=full,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _client(proc: subprocess.Popen[bytes]) -> Client:
    assert proc.stdin is not None and proc.stdout is not None
    return Client(proc.stdin, proc.stdout)


def _close(proc: subprocess.Popen[bytes]) -> int:
    assert proc.stdin is not None
    proc.stdin.close()
    try:
        return proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise


def _finish(proc: subprocess.Popen[bytes]) -> str:
    assert proc.stderr is not None
    try:
        _close(proc)
    except subprocess.TimeoutExpired:
        pass
    return proc.stderr.read().decode("utf-8", "replace")


def _kill_daemon(cache: Path) -> None:
    try:
        pid = json.loads((cache / f"server-{BUILD}.json").read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def test_the_shim_forwards_server_pushes_without_waiting_for_a_request(tmp_path: Path) -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"server-{BUILD}.json").write_text(json.dumps({
        "version": BUILD, "port": listener.getsockname()[1], "pid": os.getpid(), "token": "t",
    }), encoding="utf-8")
    hello: dict[str, Any] = {}
    push = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "yours"}}
    later = {**push, "params": {"data": "still yours"}}

    def fake_server() -> None:
        conn, _ = listener.accept()
        f = conn.makefile("rwb")
        hello.update(json.loads(f.readline()))
        f.write(b'{"ok": true, "pid": 1}\n' + json.dumps(push).encode("utf-8") + b"\n")
        f.flush()
        time.sleep(0.3)
        f.write(json.dumps(later).encode("utf-8") + b"\n")
        f.flush()
        f.write(f.readline())
        f.flush()
        f.close()
        conn.close()

    server_thread = threading.Thread(target=fake_server, daemon=True)
    server_thread.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    proc = _shim(repo, cache, CODEBASE_KG_PATH="knowledge/custom.db")
    try:
        client = _client(proc)
        assert client.recv() == push
        assert client.recv() == later
        echo = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        client.send(echo)
        assert client.recv() == echo
    finally:
        _finish(proc)
        listener.close()
    assert hello["token"] == "t"
    assert Path(hello["cwd"]).resolve() == repo.resolve()
    assert Path(hello["graph_path"]) == (repo / "knowledge" / "custom.db").resolve()


@pytest.fixture
def shim_cache(tmp_path: Path) -> Iterator[Path]:
    cache = tmp_path / "cache"
    yield cache
    _kill_daemon(cache)


def test_two_shims_share_one_daemon_end_to_end(
    two_repos: tuple[Path, Path], shim_cache: Path
) -> None:
    alpha, beta = two_repos
    procs = [_shim(alpha, shim_cache), _shim(beta, shim_cache)]
    try:
        clients = [_client(p) for p in procs]
        for c in clients:
            assert c.initialize()["result"]["serverInfo"]["name"] == "codebase-kg"
        listed = clients[0].request("tools/list", {})["result"]["tools"]
        assert {t["name"] for t in listed} >= {"kg_search", "kg_upsert_node"}
        assert _node_ids(clients[0]) == {"alpha_widget"}
        assert _node_ids(clients[1]) == {"beta_widget"}
        state = _wait_for_state(shim_cache)
    finally:
        logs = [_finish(p) for p in procs]
    pids = {line.rsplit("pid ", 1)[1].rstrip(")") for log in logs
            for line in log.splitlines() if "connected to the shared server" in line}
    assert pids == {str(state["pid"])}, logs
    assert shim.pid_alive(state["pid"]), "the daemon died with the sessions that started it"


def test_shims_started_at_once_spawn_exactly_one_daemon(
    tmp_path: Path, shim_cache: Path
) -> None:
    repo = _repo(tmp_path / "alpha", "alpha")
    procs = [_shim(repo, shim_cache) for _ in range(4)]
    try:
        clients = [_client(p) for p in procs]
        for c in clients:
            c.initialize()
            assert _node_ids(c) == {"alpha_widget"}
        state = _wait_for_state(shim_cache)
    finally:
        logs = [_finish(p) for p in procs]
    connected = [line for log in logs for line in log.splitlines()
                 if "connected to the shared server" in line]
    assert connected == [f"codebase-kg: connected to the shared server (pid {state['pid']})"] * 4, logs
    assert (shim_cache / f"server-{BUILD}.log").read_text(
        encoding="utf-8", errors="replace").count("serving codebase-kg") == 1


def test_the_shim_falls_back_to_a_private_server(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "alpha", "alpha")
    unusable = tmp_path / "not-a-dir"
    unusable.write_text("a file where the cache directory should be", encoding="utf-8")
    proc = _shim(repo, unusable, CODEBASE_KG_SHARED_TIMEOUT="2")
    try:
        client = _client(proc)
        client.initialize()
        assert _node_ids(client) == {"alpha_widget"}
    finally:
        log = _finish(proc)
    assert "running a private one" in log


def test_shared_zero_goes_straight_to_a_private_server(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "alpha", "alpha")
    cache = tmp_path / "cache"
    proc = _shim(repo, cache, CODEBASE_KG_SHARED="0")
    try:
        client = _client(proc)
        client.initialize()
        assert _node_ids(client) == {"alpha_widget"}
    finally:
        log = _finish(proc)
    assert not cache.exists()
    assert "shared server" not in log


def test_mcp_json_declares_the_http_server_with_the_headers_helper():
    root = Path(__file__).resolve().parents[2]
    entry = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["codebase-kg"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:${user_config.server_port}/mcp"
    helper = '"${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_headers.py"'
    assert entry["headersHelper"] == (
        f"command -v py >/dev/null 2>&1 && py -3 {helper} || python3 {helper} || python {helper}"
    )
    assert (root / "mcp" / "launch" / "kg_headers.py").is_file()
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["userConfig"]["server_port"]["type"] == "number"
    assert manifest["userConfig"]["server_port"]["default"] == shim.DEFAULT_HTTP_PORT
    workers = manifest["userConfig"]["max_workers"]
    assert workers == {**workers, "type": "number", "default": pool.DEFAULT_MAX_WORKERS} and workers["default"] == 8
    assert "max" not in workers and "kg_cli.py server stop" in workers["description"]
    assert "restart Claude Code" in workers["description"]


def test_the_platform_shim_launchers_stay_for_stdio_clients():
    root = Path(__file__).resolve().parents[2]
    launch = root / "mcp" / "launch"
    posix = (launch / "kg-shim").read_text(encoding="utf-8")
    windows = (launch / "kg-shim.cmd").read_bytes().decode("utf-8")
    assert '"$(dirname "$0")/../src/codebase_kg/shim.py"' in posix
    assert r"%~dp0..\src\codebase_kg\shim.py" in windows
    assert (launch / ".." / "src" / "codebase_kg" / "shim.py").resolve().is_file()
    assert posix.startswith("#!/bin/sh") and "\r\n" not in posix
    assert "py -3" in windows and "\r\n" in windows


def test_the_plugin_has_no_top_level_bin_directory():
    """claude.ai rejects a plugin with a top-level bin/ on upload and org sync,
    and Cowork refuses to install it."""
    assert not (Path(__file__).resolve().parents[2] / "bin").exists()


# --- run as a file, the shim's own folder must not shadow the stdlib ----------
def test_no_module_in_the_package_folder_shares_a_stdlib_name() -> None:
    clashes = [p.name for p in SHIM.parent.glob("*.py") if p.stem in sys.stdlib_module_names]
    assert clashes == []


def test_the_shim_run_as_a_file_skips_its_own_folder_on_sys_path(tmp_path: Path) -> None:
    folder = tmp_path / "mcp" / "src" / "codebase_kg"
    folder.mkdir(parents=True)
    shutil.copy(SHIM, folder / "shim.py")
    (folder / "selectors.py").write_text('raise ImportError("shadowed the stdlib")\n',
                                         encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["CODEBASE_KG_SHARED"] = "0"
    done = subprocess.run(
        [shutil.which("python3") or sys.executable, str(folder / "shim.py")], cwd=tmp_path,
        env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, check=False
    )
    assert b"shadowed the stdlib" not in done.stderr, done.stderr.decode("utf-8", "replace")


# --- a build is the version plus the code --------------------------------------
def test_the_build_changes_when_a_source_file_changes(tmp_path: Path) -> None:
    package = tmp_path / "codebase_kg"
    shutil.copytree(SHIM.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    before = shim.server_build(package)
    assert before.startswith(f"{shim.package_version()}+")
    assert shim.server_build(package) == before
    source = package / "tools.py"
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    touched = shim.server_build(package)
    assert touched != before
    source.write_bytes(source.read_bytes() + b"\n")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert shim.server_build(package) not in {before, touched}


def test_a_shim_of_another_build_does_not_reuse_a_running_daemon(
    daemon: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = f"{shim.package_version()}+{'0' * 12}"
    assert other != BUILD
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(daemon["cache"]))
    planted = shim.state_path(other)
    planted.write_text(json.dumps({**{k: daemon[k] for k in ("port", "pid", "token")},
                                   "version": other}), encoding="utf-8")
    spawned: list[str] = []

    class Exited:
        def poll(self) -> int:
            return 1

    def spawn(build: str) -> Exited:
        spawned.append(build)
        return Exited()

    monkeypatch.setattr(shim, "spawn_server", spawn)
    hello = {"version": other, "cwd": str(tmp_path), "graph_path": None}
    try:
        assert shim.shared_connection(other, hello, budget=5) is None
    finally:
        planted.unlink()
    assert spawned == [other]



# --- the session survives a lost server ---------------------------------------
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call(request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "kg_search", "arguments": {"query": "widget"}}}


def _result(request_id: int, **result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _initialize_results(client: Client) -> list[dict[str, Any]]:
    return [m for m in client.seen if "serverInfo" in (m.get("result") or {})]


class _Stderr:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        self._lines: list[str] = []
        self._cond = threading.Condition()
        threading.Thread(target=self._pump, args=(proc.stderr,), daemon=True).start()

    def _pump(self, stream: IO[bytes]) -> None:
        for raw in iter(stream.readline, b""):
            with self._cond:
                self._lines.append(raw.decode("utf-8", "replace"))
                self._cond.notify_all()

    def wait_for(self, text: str) -> None:
        with self._cond:
            found = self._cond.wait_for(lambda: any(text in s for s in self._lines), TIMEOUT)
        assert found, f"never saw {text!r} in:\n{self.text()}"

    def text(self) -> str:
        with self._cond:
            return "".join(self._lines)


class _FakeServer:
    def __init__(self, cache: Path) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(TIMEOUT)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"server-{BUILD}.json").write_text(json.dumps({
            "version": BUILD, "port": self.listener.getsockname()[1], "pid": os.getpid(),
            "token": "t",
        }), encoding="utf-8")
        self.opened: list[socket.socket] = []

    def accept(self) -> tuple[socket.socket, Any]:
        conn, _ = self.listener.accept()
        conn.settimeout(TIMEOUT)
        self.opened.append(conn)
        f = conn.makefile("rwb")
        assert json.loads(f.readline())["token"] == "t"
        return conn, f

    def close(self) -> None:
        for conn in self.opened:
            _hang_up(conn)
        self.listener.close()

    def end(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdin is not None
        # Stdin closes first: a hang-up while stdin is open would make the shim reconnect.
        proc.stdin.close()
        self.close()
        assert proc.wait(timeout=TIMEOUT) == 0


def _put(f: Any, obj: dict[str, Any]) -> None:
    f.write(json.dumps(obj).encode("utf-8") + b"\n")
    f.flush()


def _get(f: Any) -> dict[str, Any]:
    line = f.readline()
    assert line, "the shim closed the connection"
    return json.loads(line)


def _set_up(fake: _FakeServer, client: Client) -> tuple[socket.socket, Any]:
    conn, f = fake.accept()
    _put(f, {"ok": True, "pid": 1})
    client.send(INITIALIZE)
    assert _get(f) == INITIALIZE
    _put(f, _result(1, serverInfo={"name": "fake", "version": "0"}))
    assert client.recv()["id"] == 1
    client.send(INITIALIZED)
    assert _get(f) == INITIALIZED
    return conn, f


def test_a_request_in_flight_when_the_server_dies_gets_an_error(tmp_path: Path) -> None:
    fake = _FakeServer(tmp_path / "cache")
    proc = _shim(tmp_path, tmp_path / "cache")
    try:
        client = _client(proc)
        conn, f = _set_up(fake, client)
        client.send(_call(2))
        assert _get(f)["id"] == 2
        _hang_up(conn)
        error = client.recv()
        assert error["id"] == 2
        assert "restarted" in error["error"]["message"] and "retry" in error["error"]["message"]

        _, f2 = fake.accept()
        _put(f2, {"ok": True, "pid": 2})
        assert _get(f2) == INITIALIZE
        _put(f2, _result(1, serverInfo={"name": "fake", "version": "1"}))
        assert _get(f2) == INITIALIZED
        client.send(_call(3))
        assert _get(f2)["id"] == 3
        _put(f2, _result(3, content=[]))
        assert client.recv() == _result(3, content=[])
        assert len(_initialize_results(client)) == 1
    finally:
        fake.end(proc)


def test_messages_sent_while_reconnecting_arrive_after_the_replay(tmp_path: Path) -> None:
    fake = _FakeServer(tmp_path / "cache")
    proc = _shim(tmp_path, tmp_path / "cache")
    try:
        client = _client(proc)
        conn, _ = _set_up(fake, client)
        _hang_up(conn)
        _, f2 = fake.accept()
        client.send(_call(2))
        client.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
        client.send(_call(3))
        # Give the shim time to read these while it waits on the handshake, so they queue.
        time.sleep(0.5)
        _put(f2, {"ok": True, "pid": 2})
        assert _get(f2) == INITIALIZE
        _put(f2, _result(1, serverInfo={"name": "fake", "version": "1"}))
        assert [_get(f2) for _ in range(4)] == [
            INITIALIZED, _call(2), {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
            _call(3),
        ]
        _put(f2, _result(2, content=[]))
        _put(f2, _result(3, content=[]))
        assert [client.recv(), client.recv()] == [_result(2, content=[]), _result(3, content=[])]
        assert len(_initialize_results(client)) == 1
    finally:
        fake.end(proc)


def test_stdin_eof_ends_the_shim_without_reconnecting(tmp_path: Path) -> None:
    fake = _FakeServer(tmp_path / "cache")
    proc = _shim(tmp_path, tmp_path / "cache")
    stderr = _Stderr(proc)
    try:
        client = _client(proc)
        conn, f = _set_up(fake, client)
        client.send(_call(2))
        assert _get(f)["id"] == 2
        assert proc.stdin is not None
        proc.stdin.close()
        assert f.readline() == b""
        _hang_up(conn)
        assert proc.wait(timeout=TIMEOUT) == 0
        fake.listener.settimeout(1.0)
        with pytest.raises(TimeoutError):
            fake.listener.accept()
        assert "reconnecting" not in stderr.text()
    finally:
        if proc.poll() is None:
            proc.kill()
        fake.close()


def test_the_session_survives_a_daemon_crash(tmp_path: Path, shim_cache: Path) -> None:
    repo = _repo(tmp_path / "alpha", "alpha")
    proc = _shim(repo, shim_cache)
    stderr = _Stderr(proc)
    try:
        client = _client(proc)
        client.initialize()
        assert _node_ids(client) == {"alpha_widget"}
        old = _wait_for_state(shim_cache)["pid"]
        os.kill(old, signal.SIGTERM)
        stderr.wait_for("reconnecting")
        assert _node_ids(client) == {"alpha_widget"}
        new = _wait_for_state(shim_cache)["pid"]
        assert new != old and shim.pid_alive(new)
        stderr.wait_for(f"reconnected to the shared server (pid {new})")
        assert len(_initialize_results(client)) == 1
    finally:
        _close(proc)


def test_the_session_moves_to_a_private_server_when_no_daemon_comes_back(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "alpha", "alpha")
    cache = tmp_path / "cache"
    server_proc = _start_daemon(cache, tmp_path, {"CODEBASE_KG_IDLE_TIMEOUT": "60"})
    try:
        _wait_for_state(cache)
        proc = _shim(repo, cache)
        stderr = _Stderr(proc)
        try:
            client = _client(proc)
            client.initialize()
            assert _node_ids(client) == {"alpha_widget"}
            cache.rename(tmp_path / "cache-gone")
            cache.write_text("a file where the cache directory should be", encoding="utf-8")
            _stop(server_proc)
            stderr.wait_for("running a private one")
            assert _node_ids(client) == {"alpha_widget"}
            assert len(_initialize_results(client)) == 1
        finally:
            _close(proc)
    finally:
        _stop(server_proc)


# --- the server runs on the base interpreter, with no venv ---------------------
def test_the_server_runs_on_the_base_interpreter_without_uv() -> None:
    command = shim.server_command("--serve")
    assert command[0] == (getattr(sys, "_base_executable", None) or sys.executable)
    assert command[1:3] == ["-I", "-S"]
    assert Path(command[5]) == SRC.resolve() and command[6:] == ["--serve"]
    assert not any("uv" == Path(part).stem for part in command)


def test_the_server_imports_only_the_standard_library() -> None:
    probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); "
             "import codebase_kg.daemon; print(json.dumps(sorted(sys.modules)))")
    python = shim.server_command()[0]
    out = subprocess.run([python, "-I", "-S", "-c", probe, str(SRC)], capture_output=True, text=True,
                         timeout=TIMEOUT, check=False)
    assert out.returncode == 0, out.stderr
    modules = json.loads(out.stdout)
    assert "codebase_kg.http_transport" in modules and "codebase_kg.tcp_transport" in modules
    third_party = {"fastmcp", "mcp", "mcp_types", "pydantic", "pydantic_core", "starlette", "anyio", "uvicorn"}
    assert not [m for m in modules if m.split(".")[0] in third_party]
    assert "codebase_kg.server" not in modules


def test_a_shim_that_spawned_the_server_waits_past_the_connect_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first start after an update builds the venv, which outlasts the
    connect budget; giving up then starts a second server on the same build."""
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path))
    started = time.monotonic()

    class _Running:
        def poll(self) -> None:
            return None

    monkeypatch.setattr(shim, "spawn_server", lambda build: _Running())
    monkeypatch.setattr(shim, "SPAWN_BUDGET", 1.0)
    ready = object()
    monkeypatch.setattr(
        shim, "_try_connect",
        lambda build, hello: ready if time.monotonic() - started > 0.6 else None,
    )
    assert shim.shared_connection("0.0.0+000000000000", {}, budget=0.3) is ready


def test_a_server_whose_state_file_is_gone_or_replaced_is_orphaned(tmp_path: Path) -> None:
    from codebase_kg.daemon import Daemon

    d = Daemon("0.0.0+000000000000", "mine", 600.0)
    assert not d.orphaned()
    d.state = tmp_path / "server.json"
    assert d.orphaned()
    d.state.write_text(json.dumps({"token": "other"}), encoding="utf-8")
    assert d.orphaned()
    d.state.write_text(json.dumps({"token": "mine"}), encoding="utf-8")
    assert not d.orphaned()


def test_an_orphaned_server_stops_watching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codebase_kg import daemon

    monkeypatch.setattr(daemon, "ORPHAN_CHECK_INTERVAL", 0.0)
    d = daemon.Daemon("0.0.0+000000000000", "mine", 600.0)
    d.state = tmp_path / "server.json"
    started = time.monotonic()
    d.wait()
    assert time.monotonic() - started < 5.0


def test_a_server_whose_state_file_goes_exits_within_five_seconds(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc = _start_daemon(cache, tmp_path, {"CODEBASE_KG_IDLE_TIMEOUT": "600"})
    try:
        _wait_for_state(cache)
        (cache / f"server-{BUILD}.json").unlink()
        removed = time.monotonic()
        proc.wait(timeout=TIMEOUT)
        assert time.monotonic() - removed < 7.0
    finally:
        _stop(proc)

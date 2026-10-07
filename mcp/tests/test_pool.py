"""The shared server's elastic worker pool: grow, queue, shrink, crash, hang, and parity.

Lifecycle tests drive `Pool` with a stand-in worker that sleeps, crashes or
hangs on request. Parity tests use the real worker and compare every read tool
with the in-process result through the registered MCP surface.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

import pytest

from codebase_kg import pool as pool_mod
from codebase_kg import server, shim
from codebase_kg.pool import CallError, Pool, WorkerError
from test_query_cli import _calls

FAKE = """
import json, os, sys, time
for line in sys.stdin:
    req = json.loads(line)
    tool, args = req["tool"], req["args"]
    if tool == "crash":
        sys.exit(3)
    if tool == "hang":
        time.sleep(3600)
    if tool == "slow":
        time.sleep(args.get("s", 0.4))
    if tool == "fail":
        reply = {"ok": False, "error": "boom"}
    else:
        reply = {"ok": True, "result": {"pid": os.getpid(), "tool": tool}}
    reply["id"] = req["id"]
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def fake(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE, encoding="utf-8")
    return [sys.executable, "-I", str(script)]


@pytest.fixture
def pools() -> Iterator[list[Pool]]:
    made: list[Pool] = []
    yield made
    for p in made:
        p.close()


def _pool(pools: list[Pool], *args: Any, **kwargs: Any) -> Pool:
    p = Pool(*args, **kwargs)
    pools.append(p)
    return p


def _call(p: Pool, tool: str, **args: Any) -> dict[str, Any]:
    return p.call(tool, args, "/unused")


def _wait_until(check: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_the_pool_starts_with_no_workers(pools: list[Pool], fake: list[str]) -> None:
    assert _pool(pools, 4, command=fake).size() == 0


def test_the_pool_grows_one_worker_per_concurrent_call(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 3, command=fake)
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda _: _call(p, "slow"), range(3)))
    assert len({r["pid"] for r in results}) == 3
    assert p.size() == 3


def test_an_idle_worker_takes_the_next_call(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 4, command=fake)
    pids = {_call(p, "kg_stats")["pid"] for _ in range(5)}
    assert len(pids) == 1 and p.size() == 1


def test_calls_queue_at_the_cap(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 2, command=fake)
    sizes: list[int] = []
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            sizes.append(p.size())
            time.sleep(0.01)

    watcher = threading.Thread(target=watch)
    watcher.start()
    t = time.monotonic()
    try:
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(lambda _: _call(p, "slow", s=0.3), range(6)))
    finally:
        done.set()
        watcher.join()
    assert time.monotonic() - t >= 0.85
    assert len(results) == 6 and len({r["pid"] for r in results}) == 2
    assert max(sizes) == 2


def test_the_pool_shrinks_after_idle(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 2, idle=0.5, command=fake)
    pid = _call(p, "kg_stats")["pid"]
    assert p.size() == 1
    assert _wait_until(lambda: p.size() == 0)
    assert _wait_until(lambda: not shim.pid_alive(pid)), "the idle worker did not exit"
    assert _call(p, "kg_stats")["pid"] != pid


def test_a_crashed_worker_errors_and_is_replaced(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 2, command=fake)
    with ThreadPoolExecutor(1) as ex:
        other = ex.submit(_call, p, "slow", s=0.5)
        time.sleep(0.2)
        with pytest.raises(WorkerError, match="exited during the call"):
            _call(p, "crash")
        assert other.result()["tool"] == "slow"
    assert p.size() == 1
    assert _call(p, "kg_stats")["tool"] == "kg_stats"


def test_a_hung_worker_times_out_and_is_replaced(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 2, timeout=0.5, command=fake)
    first = _call(p, "kg_stats")["pid"]
    t = time.monotonic()
    with pytest.raises(WorkerError, match="did not answer within 0.5 s"):
        _call(p, "hang")
    assert time.monotonic() - t < 5
    assert p.size() == 0
    assert _wait_until(lambda: not shim.pid_alive(first)), "the hung worker was not killed"
    assert _call(p, "kg_stats")["pid"] != first


def test_a_tool_error_keeps_the_worker(pools: list[Pool], fake: list[str]) -> None:
    p = _pool(pools, 2, command=fake)
    pid = _call(p, "kg_stats")["pid"]
    with pytest.raises(CallError, match="^boom$"):
        _call(p, "fail")
    assert _call(p, "kg_stats")["pid"] == pid


def test_a_worker_that_cannot_start_frees_its_slot(pools: list[Pool], tmp_path: Path) -> None:
    p = _pool(pools, 1, command=[str(tmp_path / "no-such-python")])
    for _ in range(2):
        with pytest.raises(WorkerError, match="cannot start"):
            _call(p, "kg_stats")
    assert p.size() == 0


def test_close_stops_every_worker(fake: list[str]) -> None:
    p = Pool(3, command=fake)
    with ThreadPoolExecutor(3) as ex:
        pids = {r["pid"] for r in ex.map(lambda _: _call(p, "slow", s=0.2), range(3))}
    p.close()
    assert _wait_until(lambda: not any(shim.pid_alive(pid) for pid in pids))
    with pytest.raises(WorkerError, match="shut down"):
        _call(p, "kg_stats")


def test_the_max_workers_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEBASE_KG_MAX_WORKERS", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_MAX_WORKERS", raising=False)
    assert pool_mod.max_workers() == 4
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MAX_WORKERS", "2")
    assert pool_mod.max_workers() == 2
    monkeypatch.setenv("CODEBASE_KG_MAX_WORKERS", "0")
    assert pool_mod.max_workers() == 0
    monkeypatch.setenv("CODEBASE_KG_MAX_WORKERS", "lots")
    assert pool_mod.max_workers() == 2
    with pytest.raises(ValueError):
        Pool(0)


# --- the real worker, through the registered tools ----------------------------
@pytest.fixture
def bound(built_fixtures: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    graph = built_fixtures / "android" / "code_graph.db"
    monkeypatch.setattr("sys.argv", ["codebase-kg"])
    token = server.bind_connection(server.Connection(cwd=graph.parent, explicit=graph))
    try:
        yield graph
    finally:
        server.unbind_connection(token)
        server.use_pool(None)


def _through_mcp(tool: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(server.mcp.call_tool(tool, args))
    return json.loads(json.dumps(result.structured_content))


def test_every_read_tool_returns_the_same_json_from_a_worker(bound: Path, pools: list[Pool]) -> None:
    calls = _calls(bound)
    server.use_pool(None)
    in_process = [_through_mcp(tool, args) for tool, args in calls]
    p = _pool(pools, 2)
    server.use_pool(p)
    pooled = [_through_mcp(tool, args) for tool, args in calls]
    assert p.size() >= 1
    for (tool, _), here, there in zip(calls, in_process, pooled):
        assert here == there, tool


def test_a_tool_error_reads_the_same_from_a_worker(bound: Path, pools: list[Pool], tmp_path: Path) -> None:
    broken = tmp_path / "knowledge" / "code_graph.db"
    broken.parent.mkdir()
    broken.write_bytes(b"not a database at all" * 100)
    token = server.bind_connection(server.Connection(cwd=tmp_path, explicit=broken))
    try:
        server.use_pool(None)
        with pytest.raises(Exception) as here:
            server._read("kg_stats", {})
        server.use_pool(_pool(pools, 1))
        with pytest.raises(CallError) as there:
            server._read("kg_stats", {})
    finally:
        server.unbind_connection(token)
    assert str(there.value) == str(here.value)


def test_zero_workers_run_calls_in_process(bound: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a worker started with max_workers 0")

    monkeypatch.setattr(pool_mod, "_Worker", refuse)
    server.use_pool(None)
    assert _through_mcp("kg_stats", {})["nodes"] > 0


def test_the_worker_imports_no_fastmcp() -> None:
    python, *flags, _, _, src = pool_mod.worker_command()
    probe = "import sys, json; sys.path.insert(0, sys.argv[1]); import codebase_kg.worker; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([python, *flags, "-c", probe, src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    modules = json.loads(out.stdout)
    assert "codebase_kg.query" in modules
    assert not [m for m in modules if m.split(".")[0] in {"fastmcp", "mcp", "pydantic", "starlette", "anyio"}]
    assert Path(src) == Path(server.__file__).resolve().parents[1]

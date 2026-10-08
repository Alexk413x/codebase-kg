"""Compare the query CLI with the MCP server: latency, memory, concurrency, parity.

Run from mcp/ with the dev venv (it needs fastmcp for the MCP client):

    uv run python bench/cli_vs_mcp.py --graph <code_graph.db> [--out results.json]

The `mcp` mode is one stdio shim per session; the `http` mode is one shared
server reached over Streamable HTTP, started the way the SessionStart hook
starts it but with its own cache dir and an OS-picked port. Its tool calls run
on the worker pool (`--max-workers`, default the server's own default; 0 runs
them in the server process). HTTP memory is read before the first call, after
the calls, and again `--idle-wait` seconds after the last session closes, when
the pool has shrunk.

Memory figures are Windows working sets, read through the Win32 API. `cpu_pct`
is the whole machine's CPU load over a run, other programs included.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import http.client
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from typing_extensions import Self

MCP_DIR = Path(__file__).resolve().parents[1]
RUNNER = MCP_DIR / "launch" / "kg_cli.py"
SHIM = MCP_DIR / "src" / "codebase_kg" / "shim.py"
sys.path.insert(0, str(MCP_DIR / "src"))
from codebase_kg import shim

CALLS = [
    ("search", ["kg_search", '{"query": "ledger"}'], "kg_search", {"query": "ledger"}),
    ("node", ["kg_node", '{"id": "ledger_core"}'], "kg_node", {"id": "ledger_core"}),
    ("neighborhood", ["kg_neighborhood", '{"id": "ledger_core"}'], "kg_neighborhood", {"id": "ledger_core"}),
]


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _counters(handle: int) -> _Counters:
    c = _Counters()
    c.cb = ctypes.sizeof(c)
    ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb)
    return c


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


def _system_times() -> tuple[int, int]:
    """Idle and total CPU ticks since boot, summed over every CPU."""
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    ticks = [(t.high << 32) | t.low for t in (idle, kernel, user)]
    return ticks[0], ticks[1] + ticks[2]


class CpuMeter:
    """Whole-machine CPU load between `__enter__` and `__exit__`, in percent."""

    pct: float = 0.0

    def __enter__(self) -> Self:
        self._start = _system_times()
        return self

    def __exit__(self, *_: object) -> None:
        idle, total = (b - a for a, b in zip(self._start, _system_times()))
        self.pct = round(100.0 * (1 - idle / total), 1) if total else 0.0


def cpu_load(seconds: float = 3.0) -> float:
    with CpuMeter() as meter:
        time.sleep(seconds)
    return meter.pct


def _stats(ms: list[float]) -> dict[str, float]:
    s = sorted(ms)
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 1),
        "p90_ms": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
        "max_ms": round(s[-1], 1),
    }


def _cli_cmd(launcher: str, graph: str, args: list[str]) -> list[str]:
    tail = [str(RUNNER), "query", "--graph", graph, *args]
    if launcher == "uv":
        return ["uv", "run", "--no-project", "--quiet", *tail]
    return [launcher, "-I", *tail]


def cli_call(launcher: str, graph: str, args: list[str]) -> tuple[float, int, str]:
    """One CLI call: wall ms, peak working set in bytes, stdout."""
    t = time.perf_counter()
    p = subprocess.Popen(_cli_cmd(launcher, graph, args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    ms = (time.perf_counter() - t) * 1000
    if p.returncode != 0:
        raise RuntimeError(f"CLI failed ({p.returncode}): {err.decode(errors='replace')[:300]}")
    peak = _counters(int(p._handle)).PeakWorkingSetSize if launcher != "uv" else 0  # type: ignore[attr-defined]
    return ms, peak, out.decode("utf-8")


def bench_cli(launcher: str, graph: str, reps: int) -> dict:
    result: dict = {}
    peaks: list[int] = []
    for name, args, _, _ in CALLS:
        ms = []
        for _ in range(reps):
            t, peak, _ = cli_call(launcher, graph, args)
            ms.append(t)
            peaks.append(peak)
        result[name] = _stats(ms)
    if any(peaks):
        result["peak_working_set_mb"] = round(max(peaks) / 2**20, 1)
    return result


def bench_cli_concurrent(launcher: str, graph: str, workers: int) -> dict:
    t = time.perf_counter()
    with ThreadPoolExecutor(workers) as pool:
        times = list(pool.map(lambda _: cli_call(launcher, graph, CALLS[0][1])[0], range(workers)))
    return {"workers": workers, "wall_ms": round((time.perf_counter() - t) * 1000, 1), **_stats(times)}


def _process_mb(match: str, exclude: frozenset[int] = frozenset()) -> dict:
    """Working sets of running python processes whose command line contains `match`.

    `exclude` drops processes that ran before the benchmark, such as live sessions' servers.
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{match}*' }} | "
        "ForEach-Object { [pscustomobject]@{ pid = $_.ProcessId; mb = [math]::Round($_.WorkingSetSize / 1MB, 1) } } | "
        "ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False).stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    rows = [r for r in rows if r["pid"] not in exclude]
    return {"processes": len(rows), "total_mb": round(sum(r["mb"] for r in rows), 1), "pids": [r["pid"] for r in rows]}


async def bench_mcp(graph: str, reps: int, sessions: int) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    transport = lambda: StdioTransport(command=sys.executable, args=[str(SHIM)], env=env)
    result: dict = {}
    before = frozenset(_process_mb("codebase_kg")["pids"])
    result["preexisting_processes"] = len(before)
    t = time.perf_counter()
    async with Client(transport()) as c:
        result["connect_ms"] = round((time.perf_counter() - t) * 1000, 1)
        t = time.perf_counter()
        await c.call_tool(CALLS[0][2], CALLS[0][3])
        result["first_call_ms"] = round((time.perf_counter() - t) * 1000, 1)
        for name, _, tool, params in CALLS:
            ms = []
            for _ in range(reps):
                t = time.perf_counter()
                await c.call_tool(tool, params)
                ms.append((time.perf_counter() - t) * 1000)
            result[name] = _stats(ms)
        result["memory_one_session"] = _process_mb("codebase_kg", before)

    clients = [Client(transport()) for _ in range(sessions)]
    for c in clients:
        await c.__aenter__()
    try:
        result[f"memory_{sessions}_sessions"] = _process_mb("codebase_kg", before)
        t = time.perf_counter()
        await asyncio.gather(*(c.call_tool(CALLS[0][2], CALLS[0][3]) for c in clients))
        result["concurrent_first_calls"] = {"sessions": sessions, "wall_ms": round((time.perf_counter() - t) * 1000, 1)}
        t = time.perf_counter()
        await asyncio.gather(*(c.call_tool(CALLS[0][2], CALLS[0][3]) for c in clients))
        result["concurrent_warm_calls"] = {"sessions": sessions, "wall_ms": round((time.perf_counter() - t) * 1000, 1)}
    finally:
        for c in clients:
            await c.__aexit__(None, None, None)
    return result


@contextlib.contextmanager
def http_server(max_workers: int | None = None) -> Iterator[dict[str, Any]]:
    """A shared HTTP server for this run only: its own cache dir and an OS-picked port."""
    names = ("CODEBASE_KG_CACHE_DIR", "CODEBASE_KG_PORT", "CODEBASE_KG_MAX_WORKERS")
    saved = {k: os.environ.get(k) for k in names}
    os.environ["CODEBASE_KG_CACHE_DIR"] = tempfile.mkdtemp(prefix="kg-bench-")
    os.environ["CODEBASE_KG_PORT"] = "0"
    if max_workers is not None:
        os.environ["CODEBASE_KG_MAX_WORKERS"] = str(max_workers)
    build = shim.server_build()
    shim.cache_dir().mkdir(parents=True, exist_ok=True)
    before = frozenset(_process_mb("codebase_kg")["pids"])
    proc = shim.spawn_server(build)
    state: dict[str, Any] | None = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and proc.poll() is None:
            state = shim.read_state(build)
            if state and state.get("http_port"):
                break
            time.sleep(0.05)
        if not state or not state.get("http_port"):
            raise RuntimeError(f"no HTTP server; see {shim.log_path(build)}")
        yield {"port": state["http_port"], "token": shim.http_token(), "before": before}
    finally:
        if state and state.get("http_port"):
            conn = http.client.HTTPConnection("127.0.0.1", state["http_port"], timeout=5)
            with contextlib.suppress(OSError):
                conn.request("POST", "/shutdown", headers={
                    "Host": f"127.0.0.1:{state['http_port']}",
                    "Authorization": f"Bearer {state['token']}", "Content-Length": "0",
                })
                conn.getresponse().read()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def http_client(server: dict[str, Any], graph: str, client_id: str) -> Client:
    """A session as Claude Code opens one: helper headers, and the repo as its root."""
    transport = StreamableHttpTransport(f"http://127.0.0.1:{server['port']}/mcp", headers={
        "Authorization": f"Bearer {server['token']}", "X-Codebase-KG-Client": client_id,
        "X-Codebase-KG-Graph": graph,
    })
    return Client(transport, roots=[Path(graph).parents[1].as_uri()])


async def bench_http(graph: str, reps: int, sessions: int, server: dict[str, Any], idle_wait: float) -> dict:
    result: dict = {}
    before = server["before"]
    t = time.perf_counter()
    async with http_client(server, graph, "one") as c:
        result["connect_ms"] = round((time.perf_counter() - t) * 1000, 1)
        result["memory_before_calls"] = _process_mb("codebase_kg", before)
        t = time.perf_counter()
        await c.call_tool(CALLS[0][2], CALLS[0][3])
        result["first_call_ms"] = round((time.perf_counter() - t) * 1000, 1)
        for name, _, tool, params in CALLS:
            ms = []
            for _ in range(reps):
                t = time.perf_counter()
                await c.call_tool(tool, params)
                ms.append((time.perf_counter() - t) * 1000)
            result[name] = _stats(ms)
        result["memory_one_session"] = _process_mb("codebase_kg", before)

    clients = [http_client(server, graph, f"s{i}") for i in range(sessions)]
    for c in clients:
        await c.__aenter__()
    try:
        await asyncio.gather(*(c.call_tool(CALLS[0][2], CALLS[0][3]) for c in clients))
        result[f"memory_{sessions}_sessions"] = _process_mb("codebase_kg", before)
        t = time.perf_counter()
        await asyncio.gather(*(c.call_tool(CALLS[0][2], CALLS[0][3]) for c in clients))
        result["concurrent_warm_calls"] = {"sessions": sessions, "wall_ms": round((time.perf_counter() - t) * 1000, 1)}
    finally:
        for c in clients:
            await c.__aexit__(None, None, None)
    if idle_wait > 0:
        await asyncio.sleep(idle_wait)
        result[f"memory_after_{idle_wait:g}s_idle"] = _process_mb("codebase_kg", before)
    return result


async def parity(graph: str) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    out = {}
    async with Client(StdioTransport(command=sys.executable, args=[str(SHIM)], env=env)) as c:
        for name, args, tool, params in CALLS:
            mcp = (await c.call_tool(tool, params)).structured_content
            cli = json.loads(cli_call(sys.executable, graph, args)[2])
            out[name] = mcp == cli
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--python", default="python", help="interpreter for the CLI (default: python on PATH)")
    ap.add_argument("--max-workers", type=int, help="the HTTP server's pool size (default: its own default)")
    ap.add_argument("--idle-wait", type=float, default=70.0, help="seconds idle before the last HTTP memory reading")
    ap.add_argument("--modes", default="cli,mcp,http")
    ap.add_argument("--out")
    args = ap.parse_args()
    graph = str(Path(args.graph).resolve())
    modes = args.modes.split(",")

    report: dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpus": os.cpu_count(),
        "cpu_pct_before": cpu_load(),
        "graph": graph,
        "graph_mb": round(Path(graph).stat().st_size / 2**20, 2),
        "parity": asyncio.run(parity(graph)),
    }
    if "cli" in modes:
        report["cli_python"] = bench_cli(args.python, graph, args.reps)
        report["cli_uv"] = bench_cli("uv", graph, max(5, args.reps // 3))
        report["cli_concurrent"] = bench_cli_concurrent(args.python, graph, args.sessions)
    if "mcp" in modes:
        with CpuMeter() as meter:
            report["mcp"] = asyncio.run(bench_mcp(graph, args.reps, args.sessions))
        report["mcp"]["cpu_pct"] = meter.pct
    if "http" in modes:
        with http_server(args.max_workers) as server, CpuMeter() as meter:
            report["http"] = asyncio.run(bench_http(graph, args.reps, args.sessions, server, args.idle_wait))
        report["http"]["cpu_pct"] = meter.pct
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

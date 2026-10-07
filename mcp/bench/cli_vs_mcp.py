"""Compare the query CLI with the MCP server: latency, memory, concurrency, parity.

Run from mcp/ with the dev venv (it needs fastmcp for the MCP client):

    uv run python bench/cli_vs_mcp.py --graph <code_graph.db> [--out results.json]

Memory figures are Windows working sets, read through the Win32 API.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

MCP_DIR = Path(__file__).resolve().parents[1]
RUNNER = MCP_DIR / "launch" / "kg_cli.py"
SHIM = MCP_DIR / "src" / "codebase_kg" / "shim.py"

CALLS = [
    ("search", ["search", "ledger"], "kg_search", {"query": "ledger"}),
    ("node", ["node", "ledger_core"], "kg_node", {"id": "ledger_core"}),
    ("neighborhood", ["neighborhood", "ledger_core"], "kg_neighborhood", {"id": "ledger_core"}),
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
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    rows = [r for r in rows if r["pid"] not in exclude]
    return {"processes": len(rows), "total_mb": round(sum(r["mb"] for r in rows), 1), "pids": [r["pid"] for r in rows]}


async def bench_mcp(graph: str, reps: int, sessions: int) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    transport = lambda: StdioTransport(command=sys.executable, args=[str(SHIM)], env=env)  # noqa: E731
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
    ap.add_argument("--out")
    args = ap.parse_args()
    graph = str(Path(args.graph).resolve())

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "graph": graph,
        "graph_mb": round(Path(graph).stat().st_size / 2**20, 2),
        "parity": asyncio.run(parity(graph)),
        "cli_python": bench_cli(args.python, graph, args.reps),
        "cli_uv": bench_cli("uv", graph, max(5, args.reps // 3)),
        "cli_concurrent": bench_cli_concurrent(args.python, graph, args.sessions),
        "mcp": asyncio.run(bench_mcp(graph, args.reps, args.sessions)),
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

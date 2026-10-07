"""N agents at once, each running the nine lookup calls of loop10.py in order: CLI against MCP.

Each MCP agent is its own session through the shim to the shared server. Each HTTP agent is its
own client over Streamable HTTP to one shared server, as Claude Code connects: `http` runs the read
tools on the server's worker pool (`--max-workers`, default the server's default), `http0` runs them
in the server process. Each CLI agent runs one process per call. Reports wall time, per-call latency
and the whole machine's CPU load for each N.

    uv run --project <codebase-kg>/mcp python <codebase-kg>/mcp/bench/concurrency.py --graph knowledge/code_graph.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_vs_mcp import CpuMeter, cpu_load, http_client, http_server  # noqa: E402
from loop10 import CALLS, RUNNER, SHIM  # noqa: E402

LOOKUPS = [c for c in CALLS if c[0] != "kg_validate"]


def _summary(times: list[float], wall: float, errors: int) -> dict:
    s = sorted(times)
    return {
        "wall_ms": round(wall, 1),
        "call_median_ms": round(statistics.median(s), 1),
        "call_p90_ms": round(s[int(len(s) * 0.9)], 1),
        "call_max_ms": round(s[-1], 1),
        "errors": errors,
    }


def cli_agents(n: int, graph: str, python: str) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)

    def agent(_: int) -> tuple[list[float], int]:
        times, errors = [], 0
        for tool, args in LOOKUPS:
            t = time.perf_counter()
            p = subprocess.run([python, "-I", str(RUNNER), "query", tool, json.dumps(args)], capture_output=True, env=env)
            times.append((time.perf_counter() - t) * 1000)
            errors += p.returncode != 0
        return times, errors

    t = time.perf_counter()
    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(agent, range(n)))
    wall = (time.perf_counter() - t) * 1000
    return _summary([x for r in results for x in r[0]], wall, sum(r[1] for r in results))


async def mcp_agents(n: int, graph: str, server: dict | None = None) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    if server is None:
        clients = [Client(StdioTransport(command=sys.executable, args=[str(SHIM)], env=env)) for _ in range(n)]
    else:
        clients = [http_client(server, graph, f"agent{n}-{i}") for i in range(n)]
    for c in clients:
        await c.__aenter__()
        await c.call_tool("kg_stats", {})
    errors = 0

    async def agent(c: Client) -> list[float]:
        nonlocal errors
        times = []
        for tool, args in LOOKUPS:
            t = time.perf_counter()
            r = await c.call_tool(tool, args, raise_on_error=False)
            times.append((time.perf_counter() - t) * 1000)
            errors += bool(r.is_error)
        return times

    try:
        t = time.perf_counter()
        results = await asyncio.gather(*(agent(c) for c in clients))
        wall = (time.perf_counter() - t) * 1000
    finally:
        for c in clients:
            await c.__aexit__(None, None, None)
    return _summary([x for r in results for x in r], wall, errors)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--agents", default="1,4,8,16")
    ap.add_argument("--python", default="python")
    ap.add_argument("--modes", default="cli,mcp,http")
    ap.add_argument("--max-workers", type=int, help="the pool size for the http mode (default: the server's)")
    ap.add_argument("--out")
    a = ap.parse_args()
    graph = str(Path(a.graph).resolve())
    modes = a.modes.split(",")
    report: dict = {"graph": graph, "calls_per_agent": len(LOOKUPS), "cpus": os.cpu_count(),
                    "cpu_pct_before": cpu_load(), **{m: {} for m in modes}}
    agents = [int(x) for x in a.agents.split(",")]

    def timed(mode: str, n: int, run: Callable[[], dict]) -> None:
        with CpuMeter() as meter:
            report[mode][n] = run()
        report[mode][n]["cpu_pct"] = meter.pct

    for n in agents:
        if "cli" in modes:
            timed("cli", n, lambda: cli_agents(n, graph, a.python))
        if "mcp" in modes:
            timed("mcp", n, lambda: asyncio.run(mcp_agents(n, graph)))
    for mode, workers in (("http", a.max_workers), ("http0", 0)):
        if mode not in modes:
            continue
        with http_server(workers) as server:
            for n in agents:
                timed(mode, n, lambda: asyncio.run(mcp_agents(n, graph, server)))
    text = json.dumps(report, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

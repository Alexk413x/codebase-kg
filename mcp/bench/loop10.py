"""Ten different read-tool calls in a loop, timed four ways, with a parity check.

Modes:
  in-process   the tools.py function, called directly in this process
  cli          kg_cli.py query, one system-Python process per call
  mcp          one warm MCP session through the shim
  cli+server   bench/kg_client.py, one process per call, forwarded to the shared server

Run from the repo whose graph you query, with the dev venv:

    uv run --project <codebase-kg>/mcp python <codebase-kg>/mcp/bench/loop10.py --graph knowledge/code_graph.db
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
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

MCP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_DIR / "src"))
sys.path.insert(0, str(MCP_DIR / "bench"))
from codebase_kg import query  # noqa: E402
from cli_vs_mcp import _counters, _process_mb  # noqa: E402
from codebase_kg.store import CodeGraph  # noqa: E402

RUNNER = MCP_DIR / "launch" / "kg_cli.py"
CLIENT = MCP_DIR / "bench" / "kg_client.py"
SHIM = MCP_DIR / "src" / "codebase_kg" / "shim.py"

CALLS: list[tuple[str, dict]] = [
    ("kg_search", {"query": "promote run"}),
    ("kg_search", {"query": "device lease", "kind": "module"}),
    ("kg_node", {"id": "mcp_run_tools"}),
    ("kg_node", {"id": "run_map"}),
    ("kg_neighborhood", {"id": "mcp_query_tools", "depth": 1}),
    ("kg_neighborhood", {"id": "run_map", "depth": 2}),
    ("kg_find_by_kind", {"kind": "test module"}),
    ("kg_find_by_path", {"path": "mcp/src/cartographer/server.py"}),
    ("kg_find_by_kind", {"kind": "module"}),
    ("kg_find_by_reference", {}),
]


def _label(i: int) -> str:
    tool, args = CALLS[i]
    return f"{i + 1:>2} {tool} {json.dumps(args)}"


def run_inprocess(graph: str) -> list[tuple[float, dict]]:
    out = []
    for tool, args in CALLS:
        t = time.perf_counter()
        g = CodeGraph(graph)
        try:
            result = query.TOOLS[tool](g, args)
        finally:
            g.close()
        out.append(((time.perf_counter() - t) * 1000, result))
    return out


PEAKS: dict[str, list[int]] = {"cli": [], "cli+server": []}


def _proc(cmd: list[str], env: dict[str, str], mode: str) -> tuple[float, dict]:
    t = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = p.communicate()
    ms = (time.perf_counter() - t) * 1000
    PEAKS[mode].append(_counters(int(p._handle)).PeakWorkingSetSize)  # type: ignore[attr-defined]
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[-2:]} failed: {out[:200]!r} {err[:300]!r}")
    return ms, json.loads(out.decode("utf-8"))


def run_cli(graph: str, python: str) -> list[tuple[float, dict]]:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    return [_proc([python, "-I", str(RUNNER), "query", tool, json.dumps(args)], env, "cli") for tool, args in CALLS]


def run_cli_server(graph: str, python: str) -> list[tuple[float, dict]]:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    return [_proc([python, "-I", str(CLIENT), tool, json.dumps(args)], env, "cli+server") for tool, args in CALLS]


async def run_mcp(client: Client) -> list[tuple[float, dict]]:
    out = []
    for tool, args in CALLS:
        t = time.perf_counter()
        r = await client.call_tool(tool, args)
        out.append(((time.perf_counter() - t) * 1000, r.structured_content))
    return out


async def main_async(graph: str, rounds: int, python: str) -> dict:
    env = dict(os.environ, CODEBASE_KG_PATH=graph)
    modes: dict[str, list[list[tuple[float, dict]]]] = {"in-process": [], "cli": [], "mcp": [], "cli+server": []}
    before = frozenset(_process_mb("codebase_kg")["pids"])
    memory: dict = {}
    async with Client(StdioTransport(command=sys.executable, args=[str(SHIM)], env=env)) as client:
        await client.call_tool("kg_search", {"query": "warm up"})
        memory["mcp_server_idle_mb"] = _process_mb("codebase_kg", before)["total_mb"]
        for _ in range(rounds):
            modes["in-process"].append(run_inprocess(graph))
            modes["cli"].append(run_cli(graph, python))
            modes["mcp"].append(await run_mcp(client))
            modes["cli+server"].append(run_cli_server(graph, python))
        memory["mcp_server_after_loops_mb"] = _process_mb("codebase_kg", before)["total_mb"]
    memory["this_process_peak_mb"] = round(_counters(-1).PeakWorkingSetSize / 2**20, 1)
    for m, peaks in PEAKS.items():
        memory[f"{m}_per_call_peak_mb"] = {"median": round(statistics.median(peaks) / 2**20, 1),
                                           "max": round(max(peaks) / 2**20, 1)}

    report: dict = {"graph": graph, "rounds": rounds, "memory": memory, "per_call_median_ms": {},
                    "loop_of_10_ms": {}, "parity": {}}
    for i in range(len(CALLS)):
        report["per_call_median_ms"][_label(i)] = {
            m: round(statistics.median(r[i][0] for r in runs), 1) for m, runs in modes.items()
        }
        ref = modes["in-process"][0][i][1]
        report["parity"][_label(i)] = {m: runs[0][i][1] == ref for m, runs in modes.items() if m != "in-process"}
    for m, runs in modes.items():
        loops = [sum(ms for ms, _ in r) for r in runs]
        report["loop_of_10_ms"][m] = {"median": round(statistics.median(loops), 1), "min": round(min(loops), 1),
                                      "max": round(max(loops), 1)}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--python", default="python")
    ap.add_argument("--out")
    a = ap.parse_args()
    report = asyncio.run(main_async(str(Path(a.graph).resolve()), a.rounds, a.python))
    text = json.dumps(report, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

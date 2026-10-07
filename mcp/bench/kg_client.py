"""CLI that forwards one tool call to the shared codebase-kg server: kg_client.py <tool> [json-args].

Connects the way the shim does, sends one MCP tools/call, prints the structured
result, and disconnects. Stdlib only. Returns 2 when no shared server runs; it
never starts one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codebase_kg import shim  # noqa: E402


def _send(sock, message: dict) -> None:
    sock.sendall(json.dumps(message).encode("utf-8") + b"\n")


def _read_reply(sock, buf: bytes, want: int) -> tuple[dict, bytes]:
    while True:
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            message = json.loads(line)
            if message.get("id") == want:
                return message, buf
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed the connection")
        buf += chunk


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    explicit = os.environ.get("CODEBASE_KG_PATH")
    build = shim.server_build()
    hello = {"version": build, "cwd": os.getcwd(), "graph_path": str(Path(explicit).resolve()) if explicit else None}
    state = shim.read_state(build)
    if state is None or not shim.pid_alive(state["pid"]):
        print(json.dumps({"ok": False, "error": "no shared server running"}))
        return 2
    sock, buf, _ = shim.handshake(state, hello)
    try:
        _send(sock, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "kg-client", "version": "0"}}})
        _, buf = _read_reply(sock, buf, 1)
        _send(sock, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(sock, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        reply, _ = _read_reply(sock, buf, 2)
    finally:
        sock.close()
    result = reply.get("result", {})
    print(json.dumps(result.get("structuredContent", result), ensure_ascii=False))
    return 1 if result.get("isError") or "error" in reply else 0


if __name__ == "__main__":
    sys.exit(main())

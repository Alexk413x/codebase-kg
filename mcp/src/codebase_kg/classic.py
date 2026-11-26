"""Deprecated: the `initialize` handshake of the MCP revisions before 2026-07-28. Stdlib only.

MCP 2026-07-28 has no handshake: each request carries its protocol version and the
client's capabilities. This module answers clients that still open with
`initialize`: Codex over HTTP, the TCP relay's stdio clients, and the private
stdio server. It keeps no state, so every request stands alone here too.

It goes when no supported client needs it. The condition per client: the client's
documentation states 2026-07-28 support and a live test passes. Until then each
`initialize` writes one log line that names the client, so the server log shows
who still depends on this path. Stdio clients that use the bridge
(`slipspace/bridge.py`) never reach it.
"""

from __future__ import annotations

import logging
from typing import Any

from . import core, resolve, shim

logger = logging.getLogger("codebase_kg.classic")

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
NO_CWD = "codebase-kg: the request names no working directory. Send an absolute X-Codebase-KG-Cwd header."
EMPTY_LISTS = {"prompts/list": "prompts", "resources/list": "resources",
               "resources/templates/list": "resourceTemplates"}


def _client(params: dict[str, Any]) -> str:
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        return "an unnamed client"
    return f"{str(info.get('name') or 'unnamed')[:80]} {str(info.get('version') or '')[:40]}".strip()


def answer(engine: core.Core, conn: resolve.Connection | None, msg: dict[str, Any]) -> dict[str, Any] | None:
    """The response to one message of an `initialize`-first MCP session, or None for none.

    A `conn` of None is a stateless HTTP request that named no working directory.
    """
    method = msg.get("method")
    if not isinstance(method, str) or "id" not in msg:
        return None
    raw = msg.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    result: dict[str, Any]
    if method == "initialize":
        asked = params.get("protocolVersion")
        logger.info(
            "deprecated handshake: %s sent initialize for MCP %s; MCP 2026-07-28 replaces it",
            _client(params), str(asked)[:20],
        )
        result = {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": core.SERVER_INFO, "instructions": core.INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": core.CATALOG["tools"]}
    elif method in EMPTY_LISTS:
        result = {EMPTY_LISTS[method]: []}
    elif method == "tools/call":
        hooked = core.hook_result(params.get("name"), params.get("arguments"))
        if hooked is not None:
            result = hooked
        elif conn is None:
            result = core.error_result(NO_CWD)
        else:
            result = engine.call_tool(conn, params.get("name"), params.get("arguments"))
    else:
        return shim.rpc_error(msg["id"], core.METHOD_NOT_FOUND, "Method not found")
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

"""`headersHelper` for the HTTP server in `.mcp.json`: prints the request headers as JSON.

Claude Code runs this in the plugin folder on each connect attempt, so it cannot
see the session's cwd; the server asks the client for its roots instead. This
sends a random client id the server caches those roots under, the bearer token
once the server on the port proves to be this user's codebase-kg, and
`CODEBASE_KG_PATH` when set. Stdlib only, and it always prints valid JSON.

While nothing listens on the port, it waits up to `WAIT` seconds for the server
the SessionStart hook starts. Claude Code sends these headers with its next
request, and a request without the token gets 403, which Claude Code records as
"needs auth" and stops connecting for the session, and for later sessions too.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

SHIM = Path(__file__).resolve().parent.parent / "src" / "codebase_kg" / "shim.py"
WAIT = 3.0
POLL = 0.1


def _shim() -> Any:
    spec = importlib.util.spec_from_file_location("codebase_kg_shim", SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def port(shim: Any) -> int:
    try:
        found = urlsplit(os.environ.get("CLAUDE_CODE_MCP_SERVER_URL", "")).port
    except ValueError:
        found = None
    return found or shim.http_port()


def token(shim: Any, at: int, wait: float = WAIT) -> str | None:
    """The token, once the port answers as this user's codebase-kg; None if it answers as anything else."""
    deadline = time.monotonic() + wait
    while True:
        kind, _ = shim.health(at, timeout=0.5)
        if kind == shim.OURS:
            return shim.verified_token(at)
        if kind != shim.FREE or time.monotonic() >= deadline:
            return None
        time.sleep(POLL)


def headers(wait: float = WAIT) -> dict[str, str]:
    out = {"X-Codebase-KG-Client": uuid.uuid4().hex}
    graph = os.environ.get("CODEBASE_KG_PATH", "").strip()
    if graph:
        out["X-Codebase-KG-Graph"] = quote(graph, safe="/\\:")
    try:
        shim = _shim()
        found = token(shim, port(shim), wait)
    except Exception:
        found = None
    if found:
        out["Authorization"] = f"Bearer {found}"
    return out


if __name__ == "__main__":
    print(json.dumps(headers()))
    sys.exit(0)

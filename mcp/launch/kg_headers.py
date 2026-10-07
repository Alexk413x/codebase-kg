"""`headersHelper` for the HTTP server in `.mcp.json`: prints the request headers as JSON.

Claude Code runs this in the plugin folder on each connect attempt, so it cannot
see the session's cwd; the server asks the client for its roots instead. This
sends a random client id the server caches those roots under, the bearer token
once the server on the port proves to be this user's codebase-kg, and
`CODEBASE_KG_PATH` when set. Stdlib only, and it always prints valid JSON.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

SHIM = Path(__file__).resolve().parent.parent / "src" / "codebase_kg" / "shim.py"


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


def headers() -> dict[str, str]:
    out = {"X-Codebase-KG-Client": uuid.uuid4().hex}
    graph = os.environ.get("CODEBASE_KG_PATH", "").strip()
    if graph:
        out["X-Codebase-KG-Graph"] = quote(graph, safe="/\\:")
    try:
        shim = _shim()
        token = shim.verified_token(port(shim))
    except Exception:
        token = None
    if token:
        out["Authorization"] = f"Bearer {token}"
    return out


if __name__ == "__main__":
    print(json.dumps(headers()))
    sys.exit(0)

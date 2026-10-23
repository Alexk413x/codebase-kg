"""Headers helper for the shared HTTP server: prints the request headers as JSON.

Claude Code runs this as the `headersHelper` in `mcp/claude.mcp.json`, in the
plugin folder on each connect attempt, so it cannot see the session's cwd; the
server asks the client for its roots instead. This sends a random client id the
server caches those roots under, the bearer token once the server on the port
proves to be this user's codebase-kg, and `CODEBASE_KG_PATH` when set.

Codex runs it as `http_headers_helper` (see `kg_cli.py codex-config`) in the
session's folder with no variable of its own and no way to ask for roots. Every
setting is then an argument: `--port N`, `--home DIR` (the cache folder),
`--graph PATH`, `--cwd` to send the folder it runs in as `X-Codebase-KG-Cwd`,
and `--start` to start the server when nothing serves the port and to register the
session (see `register_agent`).

Stdlib only, and it always prints valid JSON.

While nothing listens on the port, or a codebase-kg on it cannot be verified yet
(a server handing the port to a newer build), it waits up to `WAIT` seconds for the
server the SessionStart hook starts, or `--start` starts. Claude Code sends these headers with its next
request, and a request without the token gets 403, which Claude Code records as
"needs auth" and stops connecting for the session, and for later sessions too.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
WAIT = 8.0
START_WAIT = 10.0
POLL = 0.1
VALUE_OPTIONS = {"--port": "CODEBASE_KG_PORT", "--home": "CODEBASE_KG_CACHE_DIR", "--graph": ""}


def _start_hook() -> Any:
    if str(HOOKS) not in sys.path:
        sys.path.insert(0, str(HOOKS))
    return importlib.import_module("kg_server_start")


def options(argv: list[str]) -> tuple[dict[str, str], set[str]]:
    """The value options and the flags in `argv`. Anything else is ignored: the helper must still print JSON."""
    values: dict[str, str] = {}
    flags: set[str] = set()
    words = iter(argv)
    for word in words:
        if word in VALUE_OPTIONS:
            values[word] = next(words, "")
        elif word in ("--cwd", "--start"):
            flags.add(word)
    return values, flags


def port(shim: Any) -> int:
    try:
        found = urlsplit(os.environ.get("CLAUDE_CODE_MCP_SERVER_URL", "")).port
    except ValueError:
        found = None
    return found or shim.http_port()


def token(shim: Any, at: int, wait: float = WAIT) -> str | None:
    """The token, once the port answers as this user's codebase-kg; None if it answers as anything else.

    A codebase-kg that answers but does not verify is a server handing over the port,
    so keep polling until the deadline.
    """
    deadline = time.monotonic() + wait
    while True:
        kind, _ = shim.health(at, timeout=0.5)
        if kind == shim.OURS:
            found = shim.verified_token(at)
            if found:
                return found
        elif kind != shim.FREE:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL)


def register_agent(shim: Any) -> None:
    """Register the Codex process that runs this helper, as `codex-<pid>`.

    Codex has no SessionEnd hook. The server drops a session whose pid has exited, so
    registering is enough. With no pid to watch, the record expires after `shim.SESSION_TTL`.
    """
    pid = shim.agent_pid()
    shim.register_session(f"codex-{pid}" if pid else "codex-unknown", pid)


def headers(
    wait: float = WAIT, graph: str | None = None, cwd: str | None = None, start: bool = False,
) -> dict[str, str]:
    out = {"X-Codebase-KG-Client": uuid.uuid4().hex}
    graph = (os.environ.get("CODEBASE_KG_PATH", "") if graph is None else graph).strip()
    if graph:
        out["X-Codebase-KG-Graph"] = quote(graph, safe="/\\:")
    if cwd:
        out["X-Codebase-KG-Cwd"] = quote(cwd, safe="/\\:")
    try:
        hook = _start_hook()
        shim = hook.load_shim()
        if start:
            try:
                register_agent(shim)
            except OSError:
                pass
            hook.ensure_server(shim, START_WAIT)
        found = token(shim, port(shim), wait)
    except Exception:  # noqa: BLE001 - any failure means no token header
        found = None
    if found:
        out["Authorization"] = f"Bearer {found}"
    return out


def main(argv: list[str]) -> None:
    values, flags = options(argv)
    for name, variable in VALUE_OPTIONS.items():
        if variable and values.get(name):
            os.environ[variable] = values[name]
    if "--port" in values:
        os.environ.pop("CLAUDE_CODE_MCP_SERVER_URL", None)
    print(json.dumps(headers(
        graph=values.get("--graph"), cwd=os.getcwd() if "--cwd" in flags else None, start="--start" in flags,
    )))


if __name__ == "__main__":
    main(sys.argv[1:])
    sys.exit(0)

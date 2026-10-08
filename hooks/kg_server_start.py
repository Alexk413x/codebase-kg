"""SessionStart hook: start the shared codebase-kg server when nothing serves its port.

Claude Code tries to connect to the HTTP server a few times in the first
seconds of a session, once more as soon as the SessionStart hooks finish, and
then gives up for the session. So this hook checks `GET /health` on the
`server_port` setting and, when nothing answers, or an older codebase-kg build
answers, starts this plugin's build detached and waits up to 3 s for it.

It always exits 0 and prints one line only when something is wrong: the port
belongs to another program, or the server did not come up in time. Stdlib only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

SHIM = Path(__file__).resolve().parents[1] / "mcp" / "src" / "codebase_kg" / "shim.py"
WAIT = 3.0
POLL = 0.1


def _shim() -> Any:
    spec = importlib.util.spec_from_file_location("codebase_kg_shim", SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serving(shim: Any, port: int, mine: dict[str, Any]) -> bool:
    """True when the port holds this build or one that outranks it."""
    kind, theirs = shim.health(port, timeout=0.5)
    return kind == shim.OURS and not shim.outranks(mine, theirs)


def ensure_server(shim: Any, wait: float = WAIT) -> str | None:
    """Start the server if it is needed. Returns the line to print, or None."""
    port = shim.http_port()
    mine = shim.build_info()
    kind, theirs = shim.health(port, timeout=0.5)
    if kind == shim.OTHER:
        return (
            f"codebase-kg: port {port} belongs to another program, so the codebase-kg tools "
            f"are unavailable. Set the plugin's server_port option to a free port."
        )
    if kind == shim.OURS and not shim.outranks(mine, theirs):
        return None
    build = mine["build"]
    shim.cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    locked = shim._take_lock(build)
    try:
        if locked:
            shim.spawn_server(build)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if _serving(shim, port, mine):
                return None
            time.sleep(POLL)
    finally:
        if locked:
            shim._release_lock(build)
    return (
        f"codebase-kg: the server did not answer on port {port} within {wait:g} s. "
        f"Its log is {shim.log_path(build)}."
    )


def main() -> None:
    try:
        sys.stdin.read()
        message = ensure_server(_shim())
    except Exception as exc:  # noqa: BLE001 - the hook reports any failure as a message
        message = f"codebase-kg: could not start the server: {exc}"
    if message:
        print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()

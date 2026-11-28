"""SessionStart hook: start the shared codebase-kg server when nothing serves its port.

Claude Code tries to connect to the HTTP server a few times in the first
seconds of a session, once more as soon as the SessionStart hooks finish, and
then gives up for the session. So this hook checks `GET /health` on the
`server_port` setting and, when nothing answers, or a build this one may replace
answers (`shim.replaces`), starts this plugin's build detached and waits up to 8 s for it. A cold
Python start takes 2 to 5 s on a loaded Windows machine.

It also registers the session with the agent's pid (`shim.register_session`). The
server drops a session whose pid has exited, so it can exit once its last
session ends and nothing has to unregister one.

It always exits 0 and prints one line only when something is wrong: the port
belongs to another program, a codebase-kg holds it that this user's state files do not name
(one in WSL answers on Windows too), the server did not come up in time, or this is a development
copy (not a release, not installed by a client) and another build holds the port. Stdlib only.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shim_loader import load_shim

WAIT = 8.0
VERIFY_WAIT = 2.0
POLL = 0.1
UNVERIFIED = "unverified"


def _serving(shim: Any, port: int, mine: dict[str, Any]) -> bool:
    """True when the port holds this build or one it may not replace."""
    kind, theirs = shim.health(port, timeout=0.5)
    return kind == shim.OURS and not shim.replaces(mine, theirs)


def _holder(shim: Any, port: int) -> tuple[str, dict[str, Any]]:
    """What holds the port. `UNVERIFIED` is a codebase-kg this user's state files never name within `VERIFY_WAIT`.

    Its token is out of reach, so every request from this session would get 403. A server that
    hands the port to another build is unverified only for a moment, hence the wait.
    """
    kind, theirs = shim.health(port, timeout=0.5)
    deadline = time.monotonic() + VERIFY_WAIT
    while kind == shim.OURS and shim.verified_holder(port) is None:
        if time.monotonic() >= deadline:
            return UNVERIFIED, theirs
        time.sleep(POLL)
        kind, theirs = shim.health(port, timeout=0.5)
    return kind, theirs


def _held(port: int, mine: dict[str, Any], theirs: dict[str, Any]) -> str | None:
    """The line for a development copy, which leaves the port to another build."""
    if mine.get("released") or mine.get("installed") or theirs.get("build") == mine.get("build"):
        return None
    return (
        f"codebase-kg: version {theirs.get('version')} ({theirs.get('build')}) holds port {port}. This copy "
        f"is a development copy (not a release, and not in a client's plugin folder), so it does not "
        f"replace that server, and this session uses it. To run this copy's own server, set the plugin's "
        f"server_port option to a free port."
    )


def ensure_server(shim: Any, wait: float = WAIT, spawn: Callable[[str], object] | None = None) -> str | None:
    """Start the server, with `spawn` or `shim.spawn_server_detached`, if it is needed. Returns the line to print, or None."""
    port = shim.http_port()
    mine = shim.build_info()
    kind, theirs = _holder(shim, port)
    if kind == shim.OTHER:
        return (
            f"codebase-kg: port {port} belongs to another program, so the codebase-kg tools "
            f"are unavailable. Set the plugin's server_port option to a free port."
        )
    if kind == UNVERIFIED:
        return (
            f"codebase-kg: port {port} holds a codebase-kg server that this session cannot use, so the "
            f"codebase-kg tools are unavailable. That server runs on another system or as another user: "
            f"WSL 2 shares its localhost ports with Windows, so a server started in WSL also answers on "
            f"Windows. Set the plugin's server_port option to another port on one of the two."
        )
    if kind == shim.OURS and not shim.replaces(mine, theirs):
        return _held(port, mine, theirs)
    build = mine["build"]
    shim.cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with shim.spawn_lock(build) as locked:
        if locked:
            (spawn or shim.spawn_server_detached)(build)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if _serving(shim, port, mine):
                return None
            time.sleep(POLL)
    return (
        f"codebase-kg: the server did not answer on port {port} within {wait:g} s. "
        f"Its log is {shim.log_path(build)}."
    )


def register(shim: Any, raw: str) -> None:
    """Record the hook payload's session, with the agent's pid, so the server can tell when it is unused."""
    try:
        session = json.loads(raw).get("session_id")
    except (ValueError, AttributeError):
        return
    if isinstance(session, str):
        shim.register_session(session, shim.agent_pid())


def main() -> None:
    try:
        raw = sys.stdin.read()
        shim = load_shim()
        try:
            register(shim, raw)
        except OSError:
            pass
        message = ensure_server(shim)
    except Exception as exc:  # noqa: BLE001 - the hook reports any failure as a message
        message = f"codebase-kg: could not start the server: {exc}"
    if message:
        print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()

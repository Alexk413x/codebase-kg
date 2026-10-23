"""Check or stop the shared server: kg_cli.py server status|stop|sessions|register|unregister.

`status` prints what answers on the HTTP port (the `server_port` setting, else
47821): the build, its pid and its `max_workers`. `stop` asks that server to
exit, with the token from its state file, and waits until the port is free. The
next session's SessionStart hook, or the next shim, starts a server again,
which reads the settings afresh.

`register --session ID [--pid N]` and `unregister --session ID` record that an
agent session uses the server, and `sessions` lists the live ones. A server that
has seen a registered session exits once none is live (see `daemon.py`). The
Claude Code hooks do this themselves; use these for an agent that has none.
Stdlib only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time

from . import cli, shim

STOP_WAIT = 10.0


def _print(data: dict[str, object]) -> int:
    print(json.dumps(data))
    return 0 if data.get("ok") else 1


def status(port: int) -> dict[str, object]:
    kind, reply = shim.health(port)
    if kind == shim.FREE:
        return {"ok": True, "port": port, "running": False}
    if kind == shim.OTHER:
        return {"ok": False, "port": port, "error": f"port {port} belongs to another program"}
    if kind == shim.UNRESPONSIVE:
        return {"ok": False, "port": port, "error": f"port {port} accepts connections but did not answer /health"}
    return {"ok": True, "port": port, "running": True, **{k: reply.get(k) for k in ("build", "pid", "max_workers")}}


def stop(port: int) -> dict[str, object]:
    holder = shim.verified_holder(port)
    if holder is None:
        found = status(port)
        if found.get("running"):
            return {"ok": False, "port": port,
                    "error": f"the server on port {port} matches no state file of this user; end pid "
                             f"{found.get('pid')} instead"}
        return {**found, "stopped": None}
    reply, state = holder
    try:
        shim.request_shutdown(port, state["token"])
    except (OSError, http.client.HTTPException) as exc:
        return {"ok": False, "port": port, "error": f"could not reach the server: {exc}"}
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        if shim.health(port, timeout=0.5)[0] == shim.FREE:
            return {"ok": True, "port": port, "running": False, "stopped": reply.get("pid")}
        time.sleep(0.1)
    return {"ok": False, "port": port, "error": f"pid {reply.get('pid')} did not exit within {STOP_WAIT:g} s"}


def sessions() -> dict[str, object]:
    return {"ok": True, "sessions": shim.live_sessions()}


def register(session: str, pid: int | None) -> dict[str, object]:
    found = pid if pid is not None else shim.agent_pid()
    if not shim.register_session(session, found):
        return {"ok": False, "error": f"{session!r} is not a valid session id"}
    return {"ok": True, "session": session, "pid": found}


def unregister(session: str) -> dict[str, object]:
    return {"ok": True, "session": session, "removed": shim.unregister_session(session)}


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(prog="kg_cli.py server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("status", "stop", "sessions", "register", "unregister"))
    ap.add_argument("--session", help="the session id, for register and unregister")
    ap.add_argument("--pid", type=int, help="the agent's pid for register (default: the nearest agent process above this one)")
    ap.add_argument("--port", type=int, help="the server's HTTP port (default: the server_port setting, else 47821)")
    ns = ap.parse_args(argv)
    port = ns.port if ns.port is not None else shim.http_port()
    if ns.action in ("register", "unregister") and not ns.session:
        ap.error(f"{ns.action} needs --session")
    if ns.action == "sessions":
        return _print(sessions())
    if ns.action == "register":
        return _print(register(ns.session, ns.pid))
    if ns.action == "unregister":
        return _print(unregister(ns.session))
    return _print(status(port) if ns.action == "status" else stop(port))


if __name__ == "__main__":
    sys.exit(main())

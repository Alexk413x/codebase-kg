"""Check or stop the shared server: kg_cli.py server status|stop.

`status` prints what answers on the HTTP port (the `server_port` setting, else
47821): the build, its pid and its `max_workers`. `stop` asks that server to
exit, with the token from its state file, and waits until the port is free. The
next session's SessionStart hook, or the next shim, starts a server again,
which reads the settings afresh. Stdlib only.
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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        conn.request("POST", "/shutdown", headers={
            "Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {state['token']}", "Content-Length": "0",
        })
        conn.getresponse().read()
    except (OSError, http.client.HTTPException) as exc:
        return {"ok": False, "port": port, "error": f"could not reach the server: {exc}"}
    finally:
        conn.close()
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        if shim.health(port, timeout=0.5)[0] == shim.FREE:
            return {"ok": True, "port": port, "running": False, "stopped": reply.get("pid")}
        time.sleep(0.1)
    return {"ok": False, "port": port, "error": f"pid {reply.get('pid')} did not exit within {STOP_WAIT:g} s"}


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()
    ap = argparse.ArgumentParser(prog="kg_cli.py server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("status", "stop"))
    ap.add_argument("--port", type=int, help="the server's HTTP port (default: the server_port setting, else 47821)")
    ns = ap.parse_args(argv)
    port = ns.port if ns.port is not None else shim.http_port()
    return _print(status(port) if ns.action == "status" else stop(port))


if __name__ == "__main__":
    sys.exit(main())

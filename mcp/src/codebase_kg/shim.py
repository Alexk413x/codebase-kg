"""The process `.mcp.json` launches for each session: a pipe to one shared server.

A server per session costs four processes — `uv`, the console-script launcher,
the venv trampoline and the interpreter, about 140 MB together on Windows — for
every open session. The shim is one small process instead. It connects to one
`codebase-kg --serve` process per machine and plugin version, starting it on
first use, and relays newline-delimited JSON-RPC between the session's stdio and
that server's socket without parsing it.

The handshake sends the shim's cwd and any explicit graph path (the first CLI
arg, else `$CODEBASE_KG_PATH`). The shared server's own cwd and environment
belong to no session, so every tool call resolves its graph from these.

If the shared server cannot be reached within `CODEBASE_KG_SHARED_TIMEOUT`
seconds (default 10), the shim runs a private stdio server as its child, so a
session never loses its tools. `CODEBASE_KG_SHARED=0` goes straight to that.
`CODEBASE_KG_CACHE_DIR` moves the state, lock and log files.

Stdlib only, and no imports from this package: `.mcp.json` runs this file with a
bare system `python3`, outside any venv. The server imports it for the paths
below, so the two sides cannot disagree about where the state file lives.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    # Run as a file, this package's folder leads sys.path, so a module here that shares
    # a stdlib name (a selectors.py, say) would shadow the stdlib one that socket imports.
    _HERE = os.path.normcase(os.path.realpath(os.path.dirname(os.path.abspath(__file__))))
    sys.path[:] = [
        p for p in sys.path if os.path.normcase(os.path.realpath(p or os.curdir)) != _HERE
    ]

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MCP_DIR = Path(__file__).resolve().parents[2]
CONNECT_BUDGET = float(os.environ.get("CODEBASE_KG_SHARED_TIMEOUT") or 10.0)
HANDSHAKE_TIMEOUT = 3.0
_POLL = 0.1
_CHUNK = 65536
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class Rejected(RuntimeError):
    """The server answered the handshake with an error."""


def cache_dir() -> Path:
    override = os.environ.get("CODEBASE_KG_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "codebase-kg"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "codebase-kg"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "codebase-kg"


def package_version(mcp_dir: Path = MCP_DIR) -> str:
    try:
        text = (mcp_dir / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match:
        return match.group(1)
    from importlib.metadata import version

    return version("codebase-kg")


def state_path(version: str) -> Path:
    return cache_dir() / f"server-{version}.json"


def lock_path(version: str) -> Path:
    return cache_dir() / f"server-{version}.lock"


def log_path(version: str) -> Path:
    return cache_dir() / f"server-{version}.log"


def read_state(version: str) -> dict[str, Any] | None:
    try:
        state = json.loads(state_path(version).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != version:
        return None
    if not all(isinstance(state.get(k), t) for k, t in (("port", int), ("pid", int), ("token", str))):
        return None
    return state


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


Connected = tuple[socket.socket, bytes, dict[str, Any]]


def handshake(state: dict[str, Any], hello: dict[str, Any]) -> Connected:
    """Connect and handshake: the socket, any bytes read past the reply, and the reply."""
    sock = socket.create_connection(("127.0.0.1", state["port"]), timeout=HANDSHAKE_TIMEOUT)
    try:
        sock.sendall(json.dumps({**hello, "token": state["token"]}).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection during the handshake")
            buf += chunk
            if len(buf) > _CHUNK:
                raise ConnectionError("handshake reply too long")
        line, _, rest = buf.partition(b"\n")
        reply = json.loads(line)
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            error = reply.get("error") if isinstance(reply, dict) else reply
            raise Rejected(str(error))
        sock.settimeout(None)
        return sock, rest, reply
    except BaseException:
        sock.close()
        raise


def _try_connect(version: str, hello: dict[str, Any]) -> Connected | None:
    state = read_state(version)
    if state is None or not pid_alive(state["pid"]):
        return None
    try:
        return handshake(state, hello)
    except (OSError, ValueError, Rejected):
        return None


def _take_lock(version: str) -> bool:
    lock = lock_path(version)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            abandoned = time.time() - lock.stat().st_mtime > CONNECT_BUDGET * 2
        except OSError:
            return False
        if abandoned:
            try:
                lock.unlink()
            except OSError:
                return False
            return _take_lock(version)
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock(version: str) -> None:
    try:
        lock_path(version).unlink()
    except OSError:
        pass


def uv_command(*extra: str) -> list[str]:
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--project", str(MCP_DIR), "--frozen", "--no-dev", "codebase-kg", *extra]


def spawn_server(version: str) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.pop("CODEBASE_KG_PATH", None)
    if sys.platform == "win32":
        # CREATE_NO_WINDOW rather than DETACHED_PROCESS: a detached uv gives each
        # console child it starts a new, visible console window.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # The host may run this shim in a job that kills its children when the
        # session closes, taking the shared server with it. Breaking away keeps it
        # alive; a job that forbids breakaway refuses the flag, so retry without.
        attempts = [flags | _CREATE_BREAKAWAY_FROM_JOB, flags]
    else:
        attempts = [0]
    with open(log_path(version), "wb") as log:
        def start(flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                uv_command("--serve"), cwd=str(cache_dir()), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                creationflags=flags, start_new_session=sys.platform != "win32",
            )

        for flags in attempts[:-1]:
            try:
                return start(flags)
            except OSError:
                pass
        return start(attempts[-1])


def shared_connection(
    version: str, hello: dict[str, Any], budget: float = CONNECT_BUDGET
) -> Connected | None:
    """Connect to the shared server, starting it if no live one answers."""
    deadline = time.monotonic() + budget
    try:
        cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return None
    locked = False
    server: subprocess.Popen[bytes] | None = None
    try:
        while time.monotonic() < deadline:
            conn = _try_connect(version, hello)
            if conn is not None:
                return conn
            if server is not None and server.poll() is not None:
                return None
            if not locked and _take_lock(version):
                locked = True
                conn = _try_connect(version, hello)
                if conn is not None:
                    return conn
                try:
                    server = spawn_server(version)
                except OSError:
                    return None
            time.sleep(_POLL)
        return None
    finally:
        if locked:
            _release_lock(version)


def _binary_stdio() -> None:
    if sys.platform == "win32":
        import msvcrt

        # A text-mode fd rewrites \n as \r\n, which corrupts frames in both directions.
        msvcrt.setmode(0, os.O_BINARY)
        msvcrt.setmode(1, os.O_BINARY)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def relay(sock: socket.socket, pending: bytes = b"") -> int:
    """Pipe stdin to the socket and the socket to stdout until the server hangs up."""
    _binary_stdio()

    def upstream() -> None:
        try:
            while True:
                data = os.read(0, _CHUNK)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        if pending:
            _write_all(1, pending)
        while True:
            data = sock.recv(_CHUNK)
            if not data:
                break
            _write_all(1, data)
    except OSError:
        pass
    finally:
        sock.close()
    return 0


def run_private(args: list[str]) -> int:
    """Today's per-session stdio server, sharing this process's stdin and stdout."""
    try:
        return subprocess.call(uv_command(*args))
    except OSError as exc:
        print(f"codebase-kg: cannot start the server: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.environ.get("CODEBASE_KG_SHARED", "").strip() == "0":
        return run_private(args)
    explicit = args[0] if args and args[0].strip() else os.environ.get("CODEBASE_KG_PATH")
    version = package_version()
    hello = {
        "version": version,
        "cwd": os.getcwd(),
        "graph_path": str(Path(explicit).resolve()) if explicit else None,
    }
    conn = shared_connection(version, hello)
    if conn is None:
        print("codebase-kg: shared server unavailable; running a private one", file=sys.stderr)
        return run_private(args)
    sock, pending, reply = conn
    print(f"codebase-kg: connected to the shared server (pid {reply.get('pid')})", file=sys.stderr)
    return relay(sock, pending)


if __name__ == "__main__":
    sys.exit(main())

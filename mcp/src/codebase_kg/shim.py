"""The process `.mcp.json` launches for each session: a pipe to one shared server.

A server per session costs four processes — `uv`, the console-script launcher,
the venv trampoline and the interpreter, about 140 MB together on Windows — for
every open session. The shim is one small process instead. It connects to one
`codebase-kg --serve` process per machine and server build, starting it on
first use, and relays newline-delimited JSON-RPC between the session's stdio and
that server's socket.

Each line passes through unchanged. The shim parses lines only to record the
session's `initialize` request and `notifications/initialized`, and which
requests still await a response. If the connection drops while the session's
stdin is open, the shim answers each request in flight with a JSON-RPC error,
reconnects the way it first connected, starting a new server if none answers,
replays the recorded setup, and then sends whatever the session wrote in the
meantime. If no shared server comes back, the rest of the session runs on a
private server, with the same replay. Stdin EOF ends the shim; it never
reconnects.

The handshake sends the shim's cwd and any explicit graph path (the first CLI
arg, else `$CODEBASE_KG_PATH`). The shared server's own cwd and environment
belong to no session, so every tool call resolves its graph from these.

A build is the package version plus a digest of the path, size and mtime of
every `*.py` in this package. A dev checkout and an installed copy at the same
version, or a working tree edited without a version bump, each get a server of
their own, so a session never talks to a server running other code.

If the shared server cannot be reached within `CODEBASE_KG_SHARED_TIMEOUT`
seconds (default 10), the shim runs a private stdio server as its child, so a
session never loses its tools. A shim that started the server itself waits up
to `CODEBASE_KG_SPAWN_TIMEOUT` seconds (default 25) instead, because the first
start after an update builds the venv. `CODEBASE_KG_SHARED=0` goes straight to
the private server. `CODEBASE_KG_CACHE_DIR` moves the state, lock and log files.

The server's venv lives outside the plugin folder, in `CODEBASE_KG_DATA_DIR`
(`.mcp.json` passes `${CLAUDE_PLUGIN_DATA}`) or else the per-user cache dir, and is keyed
by the third-party dependency set in `uv.lock`, so a plugin update that keeps
the dependencies reuses it. Builds that share the venv each import the package
from their own `src` through `PYTHONPATH`, whichever checkout the venv's
editable install points at. The server runs as `python -c`, not through the
`codebase-kg` console script: a running script's `.exe` is locked on Windows,
and another build's sync would fail to replace it.

Stdlib only, and no imports from this package: `.mcp.json` runs this file with a
bare system `python3`, outside any venv. The server imports it for the paths and
the build below, so the two sides cannot disagree about either.
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

import hashlib
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
MCP_DIR = PACKAGE_DIR.parents[1]
CONNECT_BUDGET = float(os.environ.get("CODEBASE_KG_SHARED_TIMEOUT") or 10.0)
SPAWN_BUDGET = float(os.environ.get("CODEBASE_KG_SPAWN_TIMEOUT") or 25.0)
HANDSHAKE_TIMEOUT = 3.0
REPLAY_TIMEOUT = 30.0
RETRY_DELAYS = (0.0, 1.0, 2.0)
RESTARTED = "codebase-kg shared server restarted; retry the call"
RESTARTED_CODE = -32000
_POLL = 0.1
_CHUNK = 65536
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class Rejected(RuntimeError):
    """The server answered the handshake with an error."""


def cache_dir() -> Path:
    override = os.environ.get("CODEBASE_KG_CACHE_DIR")
    return Path(override) if override else user_cache_dir()


def user_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "codebase-kg"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "codebase-kg"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "codebase-kg"


def data_dir() -> Path:
    override = os.environ.get("CODEBASE_KG_DATA_DIR")
    return Path(override) if override else user_cache_dir()


def dependency_key(mcp_dir: Path = MCP_DIR) -> str:
    """A digest of `uv.lock` without this package's own version line."""
    try:
        text = (mcp_dir / "uv.lock").read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return "unlocked"
    text = re.sub(r'(\[\[package\]\]\nname = "codebase-kg"\n)version = "[^"]*"\n', r"\1", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def venv_path() -> Path:
    return data_dir() / f"venv-{dependency_key()}"


def server_env() -> dict[str, str]:
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_path())
    src = str(MCP_DIR / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    return env


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


def server_build(package: Path = PACKAGE_DIR) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8"))
    return f"{package_version()}+{digest.hexdigest()[:12]}"


def state_path(build: str) -> Path:
    return cache_dir() / f"server-{build}.json"


def lock_path(build: str) -> Path:
    return cache_dir() / f"server-{build}.lock"


def log_path(build: str) -> Path:
    return cache_dir() / f"server-{build}.log"


def read_state(build: str) -> dict[str, Any] | None:
    try:
        state = json.loads(state_path(build).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != build:
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


def _try_connect(build: str, hello: dict[str, Any]) -> Connected | None:
    state = read_state(build)
    if state is None or not pid_alive(state["pid"]):
        return None
    try:
        return handshake(state, hello)
    except (OSError, ValueError, Rejected):
        return None


def _take_lock(build: str) -> bool:
    lock = lock_path(build)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            abandoned = time.time() - lock.stat().st_mtime > max(CONNECT_BUDGET, SPAWN_BUDGET) * 2
        except OSError:
            return False
        if abandoned:
            try:
                lock.unlink()
            except OSError:
                return False
            return _take_lock(build)
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock(build: str) -> None:
    try:
        lock_path(build).unlink()
    except OSError:
        pass


_SERVER_ENTRY = "from codebase_kg.server import main; main()"


def uv_command(*extra: str) -> list[str]:
    uv = shutil.which("uv") or "uv"
    return [
        uv, "run", "--project", str(MCP_DIR), "--frozen", "--no-dev",
        "python", "-c", _SERVER_ENTRY, *extra,
    ]


def spawn_server(build: str) -> subprocess.Popen[bytes]:
    env = server_env()
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
    with open(log_path(build), "ab") as log:
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
    build: str, hello: dict[str, Any], budget: float = CONNECT_BUDGET,
    cancel: threading.Event | None = None,
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
        while time.monotonic() < deadline and not (cancel and cancel.is_set()):
            conn = _try_connect(build, hello)
            if conn is not None:
                return conn
            if server is not None and server.poll() is not None:
                return None
            if not locked and _take_lock(build):
                locked = True
                conn = _try_connect(build, hello)
                if conn is not None:
                    return conn
                if cancel and cancel.is_set():
                    return None
                try:
                    server = spawn_server(build)
                except OSError:
                    return None
                deadline = max(deadline, time.monotonic() + SPAWN_BUDGET)
            time.sleep(_POLL)
        return None
    finally:
        if locked:
            _release_lock(build)


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


class _Lines:
    def __init__(self, data: bytes = b"") -> None:
        self._buf = bytearray(data)
        self._scanned = 0

    def feed(self, data: bytes) -> None:
        self._buf += data

    def pop(self) -> bytes | None:
        end = self._buf.find(b"\n", self._scanned)
        if end < 0:
            self._scanned = len(self._buf)
            return None
        line = bytes(self._buf[:end + 1])
        del self._buf[:end + 1]
        self._scanned = 0
        return line

    def rest(self) -> bytes:
        return bytes(self._buf)


class _Link:
    def __init__(self, pending: bytes = b"") -> None:
        self._lines = _Lines(pending)

    def line(self) -> bytes | None:
        while (line := self._lines.pop()) is None:
            try:
                data = self._recv()
            except OSError:
                return None
            if not data:
                return None
            self._lines.feed(data)
        return line

    def _recv(self) -> bytes:
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def set_timeout(self, seconds: float | None) -> None:
        pass

    def close_write(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def exit_code(self) -> int:
        return 0


class _SharedLink(_Link):
    def __init__(self, sock: socket.socket, pending: bytes = b"") -> None:
        super().__init__(pending)
        self._sock = sock

    def _recv(self) -> bytes:
        return self._sock.recv(_CHUNK)

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def set_timeout(self, seconds: float | None) -> None:
        self._sock.settimeout(seconds)

    def close_write(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self) -> None:
        self._sock.close()


class _PrivateLink(_Link):
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        super().__init__()
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None
        self._stdin = proc.stdin
        self._stdout = proc.stdout

    def _recv(self) -> bytes:
        return os.read(self._stdout.fileno(), _CHUNK)

    def send(self, data: bytes) -> None:
        self._stdin.write(data)
        self._stdin.flush()

    def close_write(self) -> None:
        try:
            self._stdin.close()
        except OSError:
            pass

    def close(self) -> None:
        self.close_write()
        self._stdout.close()

    def exit_code(self) -> int:
        return self._proc.wait()


def _messages(line: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(line)
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [m for m in items if isinstance(m, dict)]


def _id(message: dict[str, Any]) -> str | int | None:
    value = message.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return value


def _is_response(message: dict[str, Any]) -> bool:
    return "method" not in message and ("result" in message or "error" in message)


class Relay:
    def __init__(self, hello: dict[str, Any], args: list[str]) -> None:
        self._hello = hello
        self._args = args
        self._lock = threading.Lock()
        self._stdout = threading.Lock()
        self._link: _Link | None = None
        self._queue: list[tuple[bytes, list[dict[str, Any]]]] = []
        self._inflight: dict[str | int, None] = {}
        self._init: bytes | None = None
        self._init_id: str | int | None = None
        self._init_answered = False
        self._initialized: bytes | None = None
        self._eof = threading.Event()

    def run(self, link: _Link) -> int:
        _binary_stdio()
        self._link = link
        threading.Thread(target=self._read_client, daemon=True).start()
        while True:
            while (line := link.line()) is not None:
                self._to_client(line)
            link.close()
            if self._eof.is_set():
                return link.exit_code()
            recovered = self._recover()
            if recovered is None:
                return 0 if self._eof.is_set() else 1
            link = recovered

    def _read_client(self) -> None:
        lines = _Lines()
        try:
            while data := os.read(0, _CHUNK):
                lines.feed(data)
                while (line := lines.pop()) is not None:
                    self._from_client(line)
        except OSError:
            pass
        rest = lines.rest()
        if rest:
            self._from_client(rest)
        with self._lock:
            self._eof.set()
            link = self._link
        if link is not None:
            link.close_write()

    def _from_client(self, line: bytes) -> None:
        messages = _messages(line)
        with self._lock:
            link = self._link
            if link is None:
                self._queue.append((line, messages))
                return
            self._record(line, messages)
        try:
            link.send(line)
        except (OSError, ValueError):
            # A private link's pipe raises ValueError, not OSError, once it is closed.
            pass

    def _record(self, line: bytes, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            method, request_id = message.get("method"), _id(message)
            if method == "notifications/initialized" and self._initialized is None:
                self._initialized = line
            if method is None or request_id is None:
                continue
            self._inflight[request_id] = None
            if method == "initialize" and self._init is None:
                self._init, self._init_id = line, request_id

    def _to_client(self, line: bytes) -> None:
        if self._inflight:
            for message in _messages(line):
                response_id = _id(message)
                if response_id is None or not _is_response(message):
                    continue
                with self._lock:
                    self._inflight.pop(response_id, None)
                    if response_id == self._init_id:
                        self._init_answered = True
        self._write(line)

    def _write(self, data: bytes) -> None:
        with self._stdout:
            try:
                _write_all(1, data)
            except OSError:
                pass

    def _recover(self) -> _Link | None:
        with self._lock:
            self._link = None
            # An unanswered initialize stays in flight: the replay resends it, and the
            # new server's answer is the one the client is waiting for.
            lost = [request_id for request_id in self._inflight
                    if request_id != self._init_id or self._init_answered]
            for request_id in lost:
                del self._inflight[request_id]
        for request_id in lost:
            self._write(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": RESTARTED_CODE, "message": RESTARTED},
            }).encode("utf-8") + b"\n")
        print("codebase-kg: lost the server; reconnecting", file=sys.stderr)
        for delay in RETRY_DELAYS:
            if self._eof.wait(delay):
                return None
            build = server_build()
            conn = shared_connection(build, {**self._hello, "version": build}, cancel=self._eof)
            if conn is None:
                continue
            sock, pending, reply = conn
            link = _SharedLink(sock, pending)
            if self._resume(link):
                print(f"codebase-kg: reconnected to the shared server (pid {reply.get('pid')})",
                      file=sys.stderr)
                return link
            link.close()
        if self._eof.is_set():
            return None
        print("codebase-kg: shared server unavailable; running a private one", file=sys.stderr)
        try:
            proc = subprocess.Popen(uv_command(*self._args), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, env=server_env())
        except OSError as exc:
            print(f"codebase-kg: cannot start the server: {exc}", file=sys.stderr)
            return None
        private = _PrivateLink(proc)
        if self._resume(private):
            return private
        private.close()
        proc.kill()
        return None

    def _resume(self, link: _Link) -> bool:
        with self._lock:
            init, answered, initialized = self._init, self._init_answered, self._initialized
        try:
            link.set_timeout(REPLAY_TIMEOUT)
            if init is not None:
                link.send(init)
            if init is not None and answered:
                # The client already has an initialize result and must never see a
                # second one, so the new server's answer to the replay is dropped.
                while True:
                    line = link.line()
                    if line is None:
                        return False
                    if any(_id(m) == self._init_id and _is_response(m) for m in _messages(line)):
                        break
                    self._to_client(line)
            if initialized is not None:
                link.send(initialized)
            link.set_timeout(None)
        except OSError:
            return False
        with self._lock:
            queued, self._queue = self._queue, []
            try:
                for line, messages in queued:
                    self._record(line, messages)
                    link.send(line)
            except OSError:
                pass
            self._link = link
            closing = self._eof.is_set()
        if closing:
            link.close_write()
        return True


def run_private(args: list[str]) -> int:
    """Today's per-session stdio server, sharing this process's stdin and stdout."""
    try:
        return subprocess.call(uv_command(*args), env=server_env())
    except OSError as exc:
        print(f"codebase-kg: cannot start the server: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.environ.get("CODEBASE_KG_SHARED", "").strip() == "0":
        return run_private(args)
    explicit = args[0] if args and args[0].strip() else os.environ.get("CODEBASE_KG_PATH")
    build = server_build()
    hello = {
        "version": build,
        "cwd": os.getcwd(),
        "graph_path": str(Path(explicit).resolve()) if explicit else None,
    }
    conn = shared_connection(build, hello)
    if conn is None:
        print("codebase-kg: shared server unavailable; running a private one", file=sys.stderr)
        return run_private(args)
    sock, pending, reply = conn
    print(f"codebase-kg: connected to the shared server (pid {reply.get('pid')})", file=sys.stderr)
    return Relay(hello, args).run(_SharedLink(sock, pending))


if __name__ == "__main__":
    sys.exit(main())

"""The shared server's elastic pool of worker processes for read tools. Stdlib only.

One interpreter running every session's tool calls makes many agents queue
behind its lock. So under `--serve` each read tool call goes to a worker
(`worker.py`), a plain-Python process with no fastmcp, over newline-delimited
JSON on its stdin and stdout.

- An idle worker takes the call. With none idle and fewer than `limit` running,
  the pool starts one; otherwise the call waits for the next free worker.
- A worker that has had no call for `idle` seconds (default 60) is told to
  exit, so a pool with nothing to do holds no processes.
- A worker that exits during a call, or does not answer within `timeout`
  seconds (default 60), is killed and dropped. That call raises `WorkerError`;
  the next call starts a fresh worker, and other calls are not affected.

The limit is `CODEBASE_KG_MAX_WORKERS`, else the plugin's `max_workers`
setting, else 4. A server reads it once, at start. 0 means no pool: the server
runs calls in its own process.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

SRC = Path(__file__).resolve().parents[1]
DEFAULT_MAX_WORKERS = 4
IDLE_EXIT = 60.0
CALL_TIMEOUT = 60.0
STOP_WAIT = 2.0
_ENTRY = "import sys; sys.path.insert(0, sys.argv[1]); from codebase_kg.worker import main; main()"


class WorkerError(RuntimeError):
    """A worker exited or hung during a call, or could not start."""


class CallError(RuntimeError):
    """The tool raised in the worker. The message is the worker's `str(exc)`."""


def _setting(names: tuple[str, ...], default: float) -> float:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def max_workers() -> int:
    """`CODEBASE_KG_MAX_WORKERS`, else the plugin's `max_workers` setting, else 4."""
    value = _setting(("CODEBASE_KG_MAX_WORKERS", "CLAUDE_PLUGIN_OPTION_MAX_WORKERS"), DEFAULT_MAX_WORKERS)
    return max(0, int(value))


def call_timeout() -> float:
    return _setting(("CODEBASE_KG_CALL_TIMEOUT",), CALL_TIMEOUT)


def worker_command() -> list[str]:
    # A Windows venv's python.exe is a launcher that runs the base interpreter as a
    # second process; the worker needs nothing from the venv, so skip the launcher.
    python = getattr(sys, "_base_executable", None) or sys.executable
    return [python, "-I", "-S", "-c", _ENTRY, str(SRC)]


class _Worker:
    def __init__(self, command: list[str]) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=flags,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.stdin: IO[bytes] = self.proc.stdin
        self.stdout: IO[bytes] = self.proc.stdout
        self.replies: queue.Queue[bytes | None] = queue.Queue()
        self.idle_since = time.monotonic()
        threading.Thread(target=self._read, name=f"kg-worker-{self.proc.pid}", daemon=True).start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self) -> None:
        try:
            for line in self.stdout:
                self.replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)

    def call(self, request: bytes, timeout: float) -> dict[str, Any]:
        try:
            self.stdin.write(request)
            self.stdin.flush()
        except (OSError, ValueError):
            raise WorkerError(f"codebase-kg worker {self.pid} exited before the call") from None
        try:
            line = self.replies.get(timeout=timeout)
        except queue.Empty:
            raise WorkerError(
                f"codebase-kg worker {self.pid} did not answer within {timeout:g} s; it was stopped"
            ) from None
        if line is None:
            code = self.proc.poll()
            raise WorkerError(f"codebase-kg worker {self.pid} exited during the call (exit code {code})")
        try:
            reply = json.loads(line)
        except ValueError:
            raise WorkerError(f"codebase-kg worker {self.pid} sent a malformed reply") from None
        if not isinstance(reply, dict):
            raise WorkerError(f"codebase-kg worker {self.pid} sent a malformed reply")
        return reply

    def stop(self) -> None:
        """Close stdin so the worker exits on its own; kill it if it has not within `STOP_WAIT`."""
        try:
            self.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class Pool:
    """Thread-safe: each tool call blocks its own thread in `call`."""

    def __init__(
        self, limit: int, idle: float = IDLE_EXIT, timeout: float = CALL_TIMEOUT,
        command: list[str] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("a pool needs at least one worker")
        self.limit = limit
        self.idle = idle
        self.timeout = timeout
        self.command = command or worker_command()
        self._cond = threading.Condition()
        self._idle: list[_Worker] = []
        self._busy: set[_Worker] = set()
        self._starting = 0
        self._closed = False
        self._stopped = threading.Event()
        self._ids = itertools.count(1)
        self._reaper = threading.Thread(target=self._reap, name="kg-pool-reaper", daemon=True)
        self._reaper.start()

    def size(self) -> int:
        with self._cond:
            return len(self._idle) + len(self._busy) + self._starting

    def pids(self) -> list[int]:
        with self._cond:
            return sorted(w.pid for w in (*self._idle, *self._busy))

    def call(self, tool: str, args: dict[str, Any], graph: str) -> dict[str, Any]:
        request = json.dumps({"id": next(self._ids), "tool": tool, "args": args, "graph": graph})
        worker = self._acquire()
        healthy = False
        try:
            reply = worker.call(request.encode("utf-8") + b"\n", self.timeout)
            healthy = True
        finally:
            self._release(worker, healthy)
        if reply.get("ok"):
            return reply.get("result") or {}
        raise CallError(str(reply.get("error") or "the worker reported an unnamed error"))

    def _acquire(self) -> _Worker:
        with self._cond:
            while True:
                if self._closed:
                    raise WorkerError("codebase-kg worker pool is shut down")
                if self._idle:
                    worker = self._idle.pop()
                    self._busy.add(worker)
                    return worker
                if len(self._busy) + self._starting < self.limit:
                    self._starting += 1
                    break
                self._cond.wait()
        try:
            worker = _Worker(self.command)
        except OSError as exc:
            with self._cond:
                self._starting -= 1
                self._cond.notify()
            raise WorkerError(f"cannot start a codebase-kg worker: {exc}") from exc
        with self._cond:
            self._starting -= 1
            self._busy.add(worker)
        return worker

    def _release(self, worker: _Worker, healthy: bool) -> None:
        keep = healthy and worker.proc.poll() is None
        with self._cond:
            self._busy.discard(worker)
            if keep and not self._closed:
                worker.idle_since = time.monotonic()
                self._idle.append(worker)
                self._cond.notify()
                return
            self._cond.notify()
        if keep:
            worker.stop()
        else:
            worker.kill()

    def _reap(self) -> None:
        tick = max(0.05, min(1.0, self.idle / 4))
        while not self._stopped.wait(tick):
            with self._cond:
                now = time.monotonic()
                expired = [w for w in self._idle if now - w.idle_since >= self.idle]
                self._idle = [w for w in self._idle if w not in expired]
            for worker in expired:
                worker.stop()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._stopped.set()
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for worker in idle:
            worker.stop()

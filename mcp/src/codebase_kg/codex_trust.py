"""Whether Codex trusts this plugin's hooks, asked from Codex itself.

Codex runs a plugin's hooks only after the user reviewed them with `/hooks`, and
asks again after every plugin update, because its trust covers a hash of every
file in the plugin. An untrusted hook is skipped without a word, and the
SessionStart hook is one of them, so no hook can report it. `codex app-server`
answers `hooks/list` with a `trustStatus` per hook and needs no login.

The app-server is experimental. Any failure, a timeout or an answer of another
shape gives None, and the caller prints nothing. Nothing here reads or writes
Codex's config. Stdlib only.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from typing import Any

PLUGIN = "codebase-kg@"
TIMEOUT = 45.0
NEEDS_REVIEW = ("untrusted", "modified")


def _exchange(command: list[str], cwd: str, timeout: float) -> Any:
    """The result of `hooks/list` for `cwd`, or None."""
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=cwd, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def answer(wanted: int) -> Any:
        while True:
            line = lines.get(timeout=timeout)
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == wanted:
                return message.get("result")

    try:
        send({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "codebase-kg", "title": "codebase-kg", "version": "0"}}})
        if answer(0) is None:
            return None
        send({"method": "initialized"})
        send({"id": 1, "method": "hooks/list", "params": {"cwds": [cwd]}})
        return answer(1)
    except (OSError, queue.Empty):
        return None
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


def statuses(result: Any) -> list[str] | None:
    """The trust status of each enabled hook of this plugin in a `hooks/list` result, or None when the
    result has another shape."""
    try:
        found = [
            hook["trustStatus"]
            for entry in result["data"]
            for hook in entry["hooks"]
            if str(hook.get("pluginId") or "").startswith(PLUGIN) and hook.get("enabled", True)
        ]
    except (AttributeError, KeyError, TypeError):
        return None
    return found if all(isinstance(status, str) for status in found) else None


def message(found: list[str]) -> str | None:
    """What to tell the user, or None when Codex lists no hook of this plugin."""
    if not found:
        return None
    waiting = sum(status in NEEDS_REVIEW for status in found)
    if not waiting:
        return f"Codex trusts all {len(found)} codebase-kg hooks."
    return (
        f"Codex does not trust {waiting} of {len(found)} codebase-kg hooks, so they do not run: no search "
        "gate, no push gate and no session notice in Codex. The git pre-push hook still blocks a stale "
        "push. In Codex, run /hooks and trust them, or choose \"Trust all and continue\" at the start "
        "screen. Codex asks again after every plugin update."
    )


def report(cwd: str | None = None, timeout: float = TIMEOUT) -> str | None:
    codex = shutil.which("codex")
    if codex is None:
        return None
    try:
        found = statuses(_exchange([codex, "app-server"], cwd or os.getcwd(), timeout))
    except Exception:  # noqa: BLE001 - an experimental interface: no failure may reach the command
        return None
    return None if found is None else message(found)

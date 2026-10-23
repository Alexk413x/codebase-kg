"""SessionEnd hook: unregister the session, so the server can exit once its last session ends.

The server drops a session whose agent process has exited, so a hook that does not
run in time, or a crashed agent, only delays the exit. It prints nothing and
always exits 0. Stdlib only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shim_loader import load_shim


def unregister(shim: Any, raw: str) -> bool:
    try:
        session = json.loads(raw).get("session_id")
    except (ValueError, AttributeError):
        return False
    return isinstance(session, str) and shim.unregister_session(session)


def main() -> None:
    try:
        unregister(load_shim(), sys.stdin.read())
    except Exception:  # noqa: BLE001 - a session must never be held up by this
        return


if __name__ == "__main__":
    main()

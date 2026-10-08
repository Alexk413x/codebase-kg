"""Which `code_graph.db` a session's tool calls use. Stdlib only.

Order: the session's explicit graph path (the first CLI arg or
`$CODEBASE_KG_PATH` for a private server, the shim handshake's `graph_path`,
or the `X-Codebase-KG-Graph` header), then a walk up from the session's cwd
honouring an optional `graph_path` in `.claude/codebase-kg.local.md`, else
`knowledge/code_graph.db`. There is no repo-root fallback.

Only the resolved path is cached, per session, never an open handle: while
unresolved, or once the file is gone, every call resolves again, so a graph
built after the session started is found.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import cli

GRAPH_FILENAME = "code_graph.db"
# The pre-rewrite artifact. Only ever used to produce a better error message.
LEGACY_FILENAME = "KNOWLEDGE_GRAPH.md"


@dataclass
class Connection:
    """One session: where it runs, its explicit graph path, and the graph it resolved."""

    cwd: Path
    explicit: Path | None = None
    resolved: Path | None = None


def from_process(argv: list[str]) -> Connection:
    """The session of a server that serves only the process that started it."""
    if len(argv) > 1 and argv[1].strip():
        explicit: Path | None = Path(argv[1])
    elif os.environ.get("CODEBASE_KG_PATH"):
        explicit = Path(os.environ["CODEBASE_KG_PATH"])
    else:
        explicit = None
    return Connection(cwd=Path.cwd(), explicit=explicit)


def local_graph_path(base: Path) -> Path | None:
    """Honor an optional per-clone path override in `.claude/codebase-kg.local.md`.

    `graph_path` is the current key; `kg_path` is still read so an existing
    checkout keeps working across the rename.
    """
    local = base / ".claude" / "codebase-kg.local.md"
    try:
        if not local.is_file():
            return None
        lines = local.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key not in {"graph_path", "kg_path"}:
            continue
        val = re.sub(r"\s+#.*$", "", val).strip().strip("'\"")
        if val and not val.startswith("<"):
            found[key] = val
    val = found.get("graph_path") or found.get("kg_path")
    if not val:
        return None
    p = Path(val)
    return p if p.is_absolute() else base / p


def resolve(conn: Connection) -> Path | None:
    if conn.explicit is not None:
        return conn.explicit.resolve()
    for base in (conn.cwd, *conn.cwd.parents):
        override = local_graph_path(base)
        if override is not None and override.is_file():
            return override.resolve()
        cand = base / "knowledge" / GRAPH_FILENAME
        if cand.is_file():
            return cand.resolve()
    return None


def missing_graph_message(conn: Connection) -> str:
    for base in (conn.cwd, *conn.cwd.parents):
        legacy = base / "knowledge" / LEGACY_FILENAME
        if legacy.is_file():
            migrate = cli.command("migrate", f'"{legacy}"')
            return (
                f"Found a pre-rewrite {LEGACY_FILENAME} at {legacy} but no {GRAPH_FILENAME}. "
                f"Migrate it once with:\n"
                f"    {migrate}\n"
                f"then commit knowledge/{GRAPH_FILENAME}."
            )
    return (
        f"No {GRAPH_FILENAME} found. Run /codebase-kg:build to create one, pass its "
        f"path as the first CLI arg, or set $CODEBASE_KG_PATH."
    )


def graph_file(conn: Connection) -> Path:
    """The session's graph, or FileNotFoundError saying how to get one."""
    path = conn.resolved
    if path is None or not path.is_file():
        path = conn.resolved = resolve(conn)
    if path is None or not path.is_file():
        raise FileNotFoundError(missing_graph_message(conn))
    return path

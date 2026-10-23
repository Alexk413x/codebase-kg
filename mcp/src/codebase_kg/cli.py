"""Console I/O that survives a non-UTF-8 stdout. Stdlib only.

Every CLI here writes text containing em-dashes, ellipses and arrows — the
docstrings, the `…and N more` truncation line, the exported JSON. On Windows a
piped stdout defaults to the ANSI code page (cp1252), which cannot encode any of
them, so the process died with `UnicodeEncodeError` at the moment it had the
answer. `/codebase-kg:setup` hit this every time: git's textconv driver
pipes the exporter's stdout, so the diff of a committed graph was a traceback.

Reconfiguring beats prefixing `PYTHONIOENCODING=utf-8` onto every invocation,
because git spawns the textconv command itself — there is no shell in between to
carry an env var.
"""

from __future__ import annotations

import codecs
import json
import sys
from pathlib import Path
from typing import Any, TextIO

RUNNER = Path(__file__).resolve().parents[2] / "launch" / "kg_cli.py"


def command(name: str, *args: str) -> str:
    """The shell command that runs CLI `name` (build, export, migrate, upgrade).

    In the plugin, the stdlib runner beside the MCP launcher, which works from
    any repo. Without it (an installed wheel), `python -m`, which works wherever
    the package imports.
    """
    tail = " ".join(args)
    if RUNNER.is_file():
        return f'uv run --no-project --quiet "{RUNNER}" {name} {tail}'.rstrip()
    return f"python -m codebase_kg.{name} {tail}".rstrip()


def use_utf8(*streams: TextIO) -> None:
    """Force UTF-8 on the given text streams (default: stdout and stderr).

    `errors="replace"` on the way out: a mangled character in a progress line is
    strictly better than a crash that discards the work that produced it. Any
    stream that cannot be reconfigured — pytest's capture object, an already
    detached stream — is left alone rather than replaced, since the caller's
    substitute is deliberate and usually already UTF-8.
    """
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            continue


def decode_text(data: bytes) -> str:
    """Decode UTF-8, with or without a BOM, or UTF-16 when it opens with a BOM.

    PowerShell redirection writes UTF-16 with a BOM, and an editor may add a
    UTF-8 one; neither is a reason to fail or to read mojibake.
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def read_stdin() -> str:
    """All of stdin as text, decoded by `decode_text` and not by the console code page.

    On Windows a piped stdin decodes as cp1252, which turns UTF-8 input into mojibake.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return decode_text(buffer.read())


def dumps(obj: Any, **kwargs: Any) -> str:
    """`json.dumps` that keeps non-ASCII characters as they are."""
    return json.dumps(obj, ensure_ascii=False, **kwargs)


def write_out(text: str) -> None:
    """Write `text` to stdout as UTF-8 bytes, bypassing the console encoding.

    Used where the bytes themselves are the product — git textconv compares the
    exporter's stdout, so it must be exactly the UTF-8 encoding of the JSON and
    not whatever the active code page would make of it.
    """
    data = text.encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        # A capturing stand-in (pytest's capsys) has no .buffer and is already
        # text; hand it the string and let it encode.
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(data)
    buffer.flush()

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

import sys
from typing import TextIO


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

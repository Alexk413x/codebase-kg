"""Everything under `git-hooks/` is copied into other people's repos verbatim.

A machine-specific absolute path in any of those files is a defect by
construction: it names a directory that exists on exactly one computer. It also
breaks repos that guard against committed machine paths — cartographer greps
committed files for a drive letter followed by `Users`, and a comment in
`install.sh` that *illustrated* the Windows path-spelling difference turned that
repo's branch red. A guard cannot tell an example from the real thing, which is
what makes it worth having.

Two repos had re-vendored the file before the downstream guard caught it, and
fixing it downstream does nothing: the next re-vendor brings it straight back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
GIT_HOOKS = ROOT / "git-hooks"

# A drive letter is a *single* letter before the colon, which is what keeps
# `https://` and every other URL scheme out of the match.
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\/]")


def _vendored_files() -> list[Path]:
    return sorted(
        p
        for p in GIT_HOOKS.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def test_there_are_vendored_files_to_check() -> None:
    assert _vendored_files(), f"nothing found under {GIT_HOOKS}"


@pytest.mark.parametrize("path", _vendored_files(), ids=lambda p: p.name)
def test_no_drive_letter_paths(path: Path) -> None:
    hits = [
        f"{path.relative_to(ROOT).as_posix()}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DRIVE_PATH.search(line)
    ]
    assert not hits, "vendored file carries a machine-specific path:\n" + "\n".join(hits)

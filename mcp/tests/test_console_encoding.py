"""The CLIs must survive a stdout that cannot encode their own output.

Descriptions, progress lines and the exported JSON all carry em-dashes, `…` and
`→`. On Windows a piped stdout defaults to the ANSI code page, which encodes
none of them, so `codebase-kg-export` died with UnicodeEncodeError *after* doing
all the work — and git's textconv driver, which pipes exactly that stdout, made
`/codebase-kg:setup` fail on every repo with a committed graph.

`PYTHONIOENCODING=cp1252` reproduces it on any platform, so this is a real
regression test rather than a Windows-only one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg import cli
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

# Every character that has actually broken a run: em-dash, ellipsis, arrow.
SPICY = "Ranks the feed — freshest first, then per-source weight… A → B."


@pytest.fixture
def db_with_non_ascii(tmp_path: Path) -> Path:
    db = tmp_path / "code_graph.db"
    build(
        db,
        Meta(codebase="test", root="src", generated="2026-07-31"),
        [Node(
            id="feed_ranker", kind="Domain (pure)", description=SPICY,
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
    )
    return db


def _run(args: list[str], encoding: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0"}
    return subprocess.run(
        [sys.executable, "-m", *args], capture_output=True, env=env, check=False
    )


def test_export_to_stdout_survives_a_legacy_code_page(db_with_non_ascii: Path) -> None:
    proc = _run(["codebase_kg.export", str(db_with_non_ascii)], "cp1252")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    # The bytes on stdout are the product — git compares them. They must be the
    # UTF-8 encoding of the JSON, not a lossy transcode of it.
    doc = json.loads(proc.stdout.decode("utf-8"))
    assert doc["nodes"][0]["description"] == SPICY


def test_export_stdout_is_valid_utf8_under_ascii_too(db_with_non_ascii: Path) -> None:
    proc = _run(["codebase_kg.export", str(db_with_non_ascii)], "ascii")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads(proc.stdout.decode("utf-8"))["nodes"][0]["description"] == SPICY


def test_build_progress_output_survives_a_legacy_code_page(tmp_path: Path) -> None:
    """`build`'s report contains an em-dash on the no-source-tree path."""
    doc = {
        "codebase": "test", "root": "src", "generated": "2026-07-31",
        "nodes": [{
            "id": "feed_ranker", "kind": "Domain (pure)", "description": SPICY,
            "anchors": ["domain/FeedRanker.kt#FeedRanker"], "edges": [],
            "section": "DOMAIN",
        }],
    }
    src = tmp_path / "graph.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "code_graph.db"

    proc = _run(["codebase_kg.build", str(src), "-o", str(out)], "cp1252")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    assert out.is_file()


def test_use_utf8_leaves_a_stream_without_reconfigure_alone() -> None:
    """pytest's capture object has no `.reconfigure`; that must not raise."""
    class Captured:
        def write(self, s: str) -> int:
            return len(s)

    cli.use_utf8(Captured())  # type: ignore[arg-type]


def test_write_out_falls_back_when_stdout_has_no_buffer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.write_out(SPICY)
    assert capsys.readouterr().out == SPICY

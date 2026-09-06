"""`/codebase-kg:setup` end to end, driven by real git.

The exporter had unit tests and still shipped broken for the use that matters:
git spawns the textconv command *itself*, with no shell in between, and reads
its stdout as bytes. On Windows that stdout is the ANSI code page, so an
em-dash in a description killed the process and `git diff` on a committed graph
printed a traceback. Nothing in the suite ran the exporter the way git does.

These tests configure a genuine repo the way the command documents, then assert
on what `git diff` and `git show` actually print — including under
`PYTHONIOENCODING=cp1252`, which is the failure exactly as reported.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)

# Every character that has broken a run, in the text git must render.
SPICY = "Ranks the feed — freshest first, then per-source weight… A → B."


def _git(repo: Path, *args: str, encoding: str | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)}
    if encoding:
        # The console encoding git hands to the textconv child process.
        env["PYTHONIOENCODING"] = encoding
        env["PYTHONUTF8"] = "0"
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, env=env, check=False
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed:\n{proc.stderr.decode('utf-8', 'replace')}"
    )
    return proc.stdout.decode("utf-8", "replace")


def _graph(path: Path, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    build(
        path,
        Meta(codebase="test", root="src", generated="2026-08-01"),
        [Node(
            id="feed_ranker", kind="Domain", description=description,
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo wired exactly as `/codebase-kg:setup` documents."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # Step 1: the committed attribute.
    (repo / ".gitattributes").write_text(
        "code_graph.db binary diff=codegraph\n", encoding="utf-8"
    )
    # Step 2: the per-clone driver. The module form, so the test does not
    # depend on the console script being on PATH.
    _git(repo, "config", "diff.codegraph.textconv",
         f'"{sys.executable}" -m codebase_kg.export')
    _git(repo, "config", "diff.codegraph.binary", "true")

    _graph(repo / "knowledge" / "code_graph.db", SPICY)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "graph")
    return repo


def test_without_the_driver_git_says_binary_files_differ(tmp_path: Path) -> None:
    """The baseline the driver exists to replace — and proof the fixture's
    rendering below comes from textconv rather than from git guessing."""
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    _git(plain, "config", "user.email", "t@e.com")
    _git(plain, "config", "user.name", "T")
    (plain / ".gitattributes").write_text("code_graph.db binary\n", encoding="utf-8")
    _graph(plain / "knowledge" / "code_graph.db", SPICY)
    _git(plain, "add", "-A")
    _git(plain, "commit", "-q", "-m", "graph")
    _graph(plain / "knowledge" / "code_graph.db", "A different description.")

    assert "Binary files" in _git(plain, "diff", "--", "knowledge/code_graph.db")


def test_the_attribute_is_wired(repo: Path) -> None:
    assert "diff: codegraph" in _git(
        repo, "check-attr", "diff", "--", "knowledge/code_graph.db"
    )


def test_git_show_renders_the_commit_as_json(repo: Path) -> None:
    # `git show HEAD` — the commit *diff*. Note that `git show HEAD:<path>` does
    # NOT go through textconv: that form is a blob dump, so it prints raw SQLite
    # no matter how the driver is configured. Worth stating, because asserting
    # on it looks like it works — the descriptions are UTF-8 text inside the
    # file, so a substring check passes against the raw bytes and proves nothing.
    out = _git(repo, "show", "HEAD")
    assert '"id": "feed_ranker"' in out
    assert "Binary files" not in out


def test_a_changed_node_shows_up_as_a_text_diff(repo: Path) -> None:
    _graph(repo / "knowledge" / "code_graph.db", "Ranks the feed by recency only.")
    out = _git(repo, "diff", "--", "knowledge/code_graph.db")
    assert "Binary files" not in out
    assert "-" in out and "+" in out
    assert "recency only" in out          # the new description arrived
    assert "freshest first" in out        # the old one is visible as removed


def test_the_diff_survives_a_legacy_code_page(repo: Path) -> None:
    """The reported bug, through the path that reported it.

    Git spawns textconv directly — there is no shell to carry a
    `PYTHONIOENCODING=utf-8` prefix — so this has to work with the console
    encoding hostile, or `/codebase-kg:setup` is broken on Windows.
    """
    _graph(repo / "knowledge" / "code_graph.db", "Ranks the feed by recency only.")
    out = _git(repo, "diff", "--", "knowledge/code_graph.db", encoding="cp1252")
    assert "UnicodeEncodeError" not in out
    assert "Binary files" not in out
    assert "recency only" in out


def test_non_ascii_descriptions_render_intact_under_cp1252(repo: Path) -> None:
    """Not merely "did not crash": the characters must survive the round trip."""
    out = _git(repo, "show", "HEAD", encoding="cp1252")
    # The JSON marker first — without it, a raw blob dump would satisfy the
    # SPICY check on its own and the test would pass while proving nothing.
    assert '"id": "feed_ranker"' in out
    assert SPICY in out


def test_log_p_renders_every_revision(repo: Path) -> None:
    """`cachetextconv` and `log -p` walk multiple blobs; one bad blob used to
    take down the whole traversal."""
    _git(repo, "config", "diff.codegraph.cachetextconv", "true")
    _graph(repo / "knowledge" / "code_graph.db", "Second revision — still spicy…")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "refresh")

    out = _git(repo, "log", "-p", "--", "knowledge/code_graph.db", encoding="cp1252")
    assert "Binary files" not in out
    assert "Second revision" in out
    assert "freshest first" in out

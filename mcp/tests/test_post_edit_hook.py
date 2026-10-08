"""The PostToolUse nudge — the most-run code in the plugin, previously untested.

`hooks/kg_post_edit_check.py` fires after every Edit/Write in every
repo that installs the plugin, and no test imported it. `coverage.py` reported
it as `never imported` while the package around it sat at 91%.

Two properties dominate, in this order:

1. **It never breaks an edit.** Any failure — malformed payload, unreadable
   graph, unwritable state — must exit silently. A nudge that costs someone
   their edit is worse than no nudge.
2. **It nudges on fact, not on guesswork.** Signal 1 ("no node anchors this
   file") is a claim about the graph and must be true when made, and made only
   once per file.

State lives in the OS temp dir, so every test redirects `tempfile.gettempdir`
to its own `tmp_path` — otherwise tests would share a counter with each other
*and* with the developer's real repos.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import kg_post_edit_check as hook


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test hook state, never the shared temp dir."""
    state = tmp_path / "hookstate"
    state.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(state))
    # CLAUDE_PROJECT_DIR outranks the payload's cwd; a developer's real value
    # would silently redirect every test at their own repo.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project with a graph anchoring exactly one file."""
    proj = tmp_path / "proj"
    (proj / "src" / "domain").mkdir(parents=True)
    (proj / "src" / "domain" / "FeedRanker.kt").write_text("x", encoding="utf-8")
    (proj / "src" / "domain" / "Unmapped.kt").write_text("x", encoding="utf-8")
    (proj / "knowledge").mkdir()
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="test", root="src", generated="2026-08-01"),
        [Node(
            id="feed_ranker", kind="Domain", description="Ranks the feed.",
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
        source_root=proj / "src",
    )
    return proj


def _payload(repo: Path, rel: str, tool: str = "Edit") -> str:
    return json.dumps({
        "tool_name": tool,
        "tool_input": {"file_path": str(repo / rel)},
        "cwd": str(repo),
    })


def _run(payload: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload))
    hook.main()
    return ""


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# --- property 1: it never breaks an edit -------------------------------------
@pytest.mark.parametrize("payload", [
    "",                                   # no stdin at all
    "not json",                           # malformed
    "[]",                                 # valid JSON, wrong shape
    '{"tool_name": "Edit"}',              # no tool_input
    '{"tool_name": "Edit", "tool_input": "nope"}',   # tool_input not a dict
    '{"tool_name": "Edit", "tool_input": {}}',       # no file_path
    '{"tool_name": "Edit", "tool_input": {"file_path": 42}}',  # wrong type
])
def test_a_broken_payload_is_silent_and_does_not_raise(
    payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(payload, monkeypatch)
    assert _emitted(capsys) == []


def test_an_unreadable_graph_does_not_raise(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "knowledge" / "code_graph.db").write_text("junk", encoding="utf-8")
    _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)
    # `_rows` turns any sqlite failure into "no rows", so the file reads as
    # unanchored and the nudge is still honest about what it can see.
    assert len(_emitted(capsys)) <= 1


def test_unwritable_state_does_not_raise(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", boom)
    _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)  # must not raise


# --- which events are even considered ----------------------------------------
@pytest.mark.parametrize("tool", ["Read", "Bash", "Grep", "Task", ""])
def test_non_edit_tools_are_ignored(
    repo: Path, tool: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(_payload(repo, "src/domain/Unmapped.kt", tool=tool), monkeypatch)
    assert _emitted(capsys) == []


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_every_edit_tool_is_considered(
    repo: Path, tool: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(_payload(repo, "src/domain/Unmapped.kt", tool=tool), monkeypatch)
    assert len(_emitted(capsys)) == 1


def test_a_repo_with_no_graph_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare"
    (bare / "src").mkdir(parents=True)
    (bare / "src" / "A.kt").write_text("x", encoding="utf-8")
    _run(_payload(bare, "src/A.kt"), monkeypatch)
    assert _emitted(capsys) == []


def test_the_off_switch_is_honoured(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude" / "codebase-kg.local.md").write_text(
        "---\npost_edit_nudge: false\n---\n", encoding="utf-8"
    )
    _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)
    assert _emitted(capsys) == []


# --- signal 1: this file is not in the map -----------------------------------
def test_an_unmapped_file_is_reported_with_its_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)
    (msg,) = _emitted(capsys)
    text = msg["systemMessage"]
    assert "domain/Unmapped.kt" in text          # relative to `root`, as anchored
    assert "/codebase-kg:refresh" in text
    assert "suppressOutput" not in msg


def test_an_anchored_file_is_not_reported_as_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(_payload(repo, "src/domain/FeedRanker.kt"), monkeypatch)
    assert _emitted(capsys) == []


def test_the_same_unmapped_file_is_reported_only_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(4):
        _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)
    # Once, not four times — a hook that repeats itself gets muted by the reader.
    assert len(_emitted(capsys)) == 1


def test_a_file_outside_the_root_is_ignored(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.kt").write_text("x", encoding="utf-8")
    _run(_payload(repo, "scripts/tool.kt"), monkeypatch)
    assert _emitted(capsys) == []


def test_a_doc_edit_is_not_a_source_edit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "src" / "NOTES.md").write_text("x", encoding="utf-8")
    _run(_payload(repo, "src/NOTES.md"), monkeypatch)
    assert _emitted(capsys) == []


# --- signal 2: the periodic drift nudge --------------------------------------
def test_the_periodic_nudge_fires_on_the_configured_interval(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude" / "codebase-kg.local.md").write_text(
        "---\nnudge_every: 2\n---\n", encoding="utf-8"
    )
    # An anchored file, so signal 1 never fires and only the counter is at play.
    for _ in range(4):
        _run(_payload(repo, "src/domain/FeedRanker.kt"), monkeypatch)
    messages = _emitted(capsys)
    assert len(messages) == 2  # on the 2nd and 4th edit
    assert "2 source edit(s)" in messages[0]["systemMessage"]
    assert "4 source edit(s)" in messages[1]["systemMessage"]


def test_editing_the_graph_clears_the_counter(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude" / "codebase-kg.local.md").write_text(
        "---\nnudge_every: 2\n---\n", encoding="utf-8"
    )
    _run(_payload(repo, "src/domain/FeedRanker.kt"), monkeypatch)
    # A refresh rewrites the graph: the pending count resets, so the next edit
    # is the first one again rather than tripping the interval immediately.
    _run(_payload(repo, "knowledge/code_graph.db"), monkeypatch)
    _run(_payload(repo, "src/domain/FeedRanker.kt"), monkeypatch)
    assert _emitted(capsys) == []


@pytest.mark.parametrize("value, edits_before_first_nudge", [
    ("0", 5),    # falsy => `or 5` restores the default rather than dividing by zero
    ("-3", 1),   # negative => max(1, …) clamps to "every edit"
    ("junk", 5), # uncoercible => same fallback, no traceback
])
def test_a_degenerate_nudge_every_never_raises(
    repo: Path, value: str, edits_before_first_nudge: int,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The modulo operand comes straight from user config, so it must be total.

    `0` is not clamped to 1 as one might expect — a falsy value reads as unset
    and restores the default. Pinned because the two plausible readings differ,
    and only one of them is what ships.

    `junk` used to raise inside `int()`, which `main` swallowed: one typo in
    `.local.md` disabled the nudge for that repo permanently, silently, with the
    hook still reporting success. Found by this test, fixed in `_as_int`.
    """
    (repo / ".claude").mkdir()
    (repo / ".claude" / "codebase-kg.local.md").write_text(
        f"---\nnudge_every: {value}\n---\n", encoding="utf-8"
    )
    for _ in range(edits_before_first_nudge):
        _run(_payload(repo, "src/domain/FeedRanker.kt"), monkeypatch)
    assert len(_emitted(capsys)) == 1


def test_state_is_written_outside_the_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook must never write into the user's working tree."""
    before = {p for p in repo.rglob("*") if p.is_file()}
    _run(_payload(repo, "src/domain/Unmapped.kt"), monkeypatch)
    assert {p for p in repo.rglob("*") if p.is_file()} == before

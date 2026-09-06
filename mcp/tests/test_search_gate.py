"""The PreToolUse search gate — the hook that makes the graph get used at all.

`hooks/kg_search_gate.py` is the only piece of the plugin that can *deny* a tool
call, so it carries a heavier burden than the advisory nudge beside it. Three
properties dominate, in this order:

1. **It can never strand an agent.** One interruption per session, spent whether
   the agent complies or not, and a malformed payload or unwritable state fails
   open. A gate that loops is worse than no gate.
2. **It fires on the search, not on the neighbors.** `Grep`/`Glob` always; a
   shell `grep`/`rg`/`find -name` too; an ordinary `git status` never.
3. **It stands down the moment the graph is consulted**, so an agent that
   started where it should never sees it.

State is per project + session in the OS temp dir, so every test redirects
`tempfile.gettempdir` — otherwise tests would share a gate with each other and
with the developer's real repos.
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

import kg_search_gate as gate  # noqa: E402


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test gate state, never the shared temp dir."""
    state = tmp_path / "gatestate"
    state.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(state))
    # CLAUDE_PROJECT_DIR outranks the payload's cwd; a developer's real value
    # would silently redirect every test at their own repo.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project with a committed graph rooted at `app/src`."""
    proj = tmp_path / "repo"
    (proj / "knowledge").mkdir(parents=True)
    (proj / "app" / "src" / "domain").mkdir(parents=True)
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="test", root="app/src", generated="2026-08-01"),
        [Node(
            id="feed_ranker", kind="Domain", description="Ranks the feed.",
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
    )
    return proj


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proj: Path,
    tool: str,
    tool_input: dict[str, Any] | None = None,
    session: str = "s1",
) -> dict[str, Any] | None:
    """Drive the hook end-to-end and return its parsed stdout, or None if silent."""
    payload = {
        "tool_name": tool,
        "tool_input": tool_input or {},
        "cwd": str(proj),
        "session_id": session,
    }
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(payload)))
    gate.main()
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def decision(result: dict[str, Any] | None) -> str | None:
    if not result:
        return None
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


# --- the gate itself ---------------------------------------------------------
def test_the_first_grep_of_a_session_is_denied_with_instructions(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "FeedRanker"})
    assert decision(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
    assert "code_graph.db" in reason
    assert "kg_search" in reason


def test_glob_is_gated_too(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert decision(run(monkeypatch, capsys, repo, "Glob", {"pattern": "**/*.kt"})) == "deny"


def test_the_gate_stands_down_after_one_denial(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The single most important property: it must not be possible to loop.

    The agent may decide the graph cannot answer this one. It still gets the
    search — the gate's job is to make sure the graph was offered, not to win.
    """
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})) == "deny"
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None
    assert run(monkeypatch, capsys, repo, "Glob", {"pattern": "*.kt"}) is None


def test_a_new_session_is_gated_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}, session="s1")
    assert decision(
        run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}, session="s2")
    ) == "deny"


def test_a_graph_query_stands_the_gate_down(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent that already started at the graph must never see the gate."""
    assert run(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"q": "feed"}) is None
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__codebase-kg__kg_node",
        "mcp__plugin_codebase-kg_codebase-kg__kg_search",
        "mcp__codebase_kg__kg_neighborhood",
    ],
)
def test_every_shape_of_the_servers_name_counts_as_a_query(
    tool: str, repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The host names the server differently standalone vs. as a plugin. Missing
    one shape means the gate fires at an agent that did everything right."""
    run(monkeypatch, capsys, repo, tool, {})
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


def test_another_plugins_mcp_call_does_not_stand_it_down(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run(monkeypatch, capsys, repo, "mcp__github__search_code", {})
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})) == "deny"


# --- when it must stay out of the way ----------------------------------------
def test_a_repo_without_a_graph_is_never_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert run(monkeypatch, capsys, bare, "Grep", {"pattern": "x"}) is None


def test_skip_kg_silences_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SKIP_KG", "1")
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


def _write_local_config(proj: Path, body: str) -> None:
    (proj / ".claude").mkdir(exist_ok=True)
    (proj / ".claude" / "codebase-kg.local.md").write_text(
        f"---\n{body}\n---\n", encoding="utf-8"
    )


def test_search_gate_off_silences_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`off` reaches the gate as a bool, not the string — the parser coerces it."""
    _write_local_config(repo, "search_gate: off")
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


def test_warn_mode_advises_instead_of_denying(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_local_config(repo, "search_gate: warn")
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert decision(result) is None
    assert "kg_search" in result["systemMessage"]  # type: ignore[index]
    # Still one interruption per session.
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


@pytest.mark.parametrize("raw", ["block", "BLOCK", "nonsense", "true"])
def test_an_unknown_mode_keeps_the_gate_on(raw: str) -> None:
    """A typo should cost the setting, not the feature — the same posture as
    `nudge_every`."""
    assert gate.gate_mode({"search_gate": raw}) == "block"


@pytest.mark.parametrize(
    ("path", "gated"),
    [
        ("app/src", True),          # inside root
        ("app/src/domain", True),   # deeper inside root
        ("app", True),              # a parent of root still contains mapped code
        ("docs", False),            # elsewhere in the repo
        ("node_modules", False),    # ignored dir
        ("", True),                 # unscoped
    ],
)
def test_only_searches_that_can_reach_mapped_code_are_gated(
    path: str, gated: bool, repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool_input: dict[str, Any] = {"pattern": "x"}
    if path:
        tool_input["path"] = str(repo / path)
    result = run(monkeypatch, capsys, repo, "Grep", tool_input)
    assert (decision(result) == "deny") is gated


# --- shell searches ----------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        "grep -rn FeedRanker .",
        "rg --files-with-matches Ranker",
        "cat x.txt | grep foo",
        "find . -name '*.kt'",
        "find app -iname FeedRanker.kt",
        "Get-ChildItem -Recurse -Filter *.kt",
        "sudo grep -r secret /etc",
    ],
)
def test_a_shell_search_counts_as_a_search(command: str) -> None:
    assert gate.is_shell_search(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git status --porcelain",
        "python -m pytest",
        "find . -type d",            # no name filter → a listing, not a search
        "ls -la hooks/",
        "npm run build",
        "git log --grep is not our grep",  # `--grep` is a flag, not the command
    ],
)
def test_an_ordinary_command_is_not_a_search(command: str) -> None:
    """False positives are the expensive direction: they deny work the graph
    could never have answered."""
    assert gate.is_shell_search(command) is False


def test_a_shell_search_is_gated_end_to_end(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(monkeypatch, capsys, repo, "Bash", {"command": "grep -rn Feed ."})
    assert decision(result) == "deny"


def test_gate_shell_search_false_leaves_the_shell_alone(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_local_config(repo, "gate_shell_search: false")
    assert run(monkeypatch, capsys, repo, "Bash", {"command": "grep -rn Feed ."}) is None
    # The real search tools stay gated.
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})) == "deny"


# --- it must never break a session -------------------------------------------
@pytest.mark.parametrize(
    "raw", ["", "   ", "not json", "[]", '{"tool_name": 5}', '{"tool_input": "nope"}']
)
def test_a_malformed_payload_is_silent(
    raw: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(raw))
    gate.main()
    assert capsys.readouterr().out.strip() == ""


def test_it_fails_open_when_state_cannot_be_written(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unwritable temp dir must not deny twice — it denies once and the write
    failure is swallowed, so the worst case is the message repeating, never a
    crash."""
    def boom(*_: object, **__: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", boom)
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert decision(result) == "deny"


def test_an_unreadable_graph_does_not_raise(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "knowledge" / "code_graph.db").write_bytes(b"not a database")
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert decision(result) == "deny"  # root unreadable → treated as the whole repo

"""The PreToolUse search gate — the hook that makes the graph get used at all.

`hooks/kg_search_gate.py` is the only piece of the plugin that can *deny* a tool
call, so it carries a heavier burden than the advisory nudge beside it. Three
properties dominate, in this order:

1. **It can never strand an agent.** An immediate repeat of the same search is
   always allowed, a malformed payload is silent, and a state file it cannot
   write degrades the deny to a warn — because an unrecordable denial is one the
   repeat could not be recognised against. A gate that loops is worse than none.
2. **It fires on the search, not on the neighbors.** `Grep`/`Glob` always; a
   shell `grep`/`rg`/`find -name` too; an ordinary `git status` never.
3. **It keeps asking.** Unlike the first version it does not stand down for the
   session after one nudge; a graph query buys `gate_credit` searches, and a
   search already scoped to an anchored file is never gated at all.

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


def test_repeating_the_same_search_is_always_allowed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The single most important property: it must not be possible to loop.

    The agent may decide the graph cannot answer this one. Asking again is how
    it says so, and that always works — the gate's job is to make sure the graph
    was offered, not to win.
    """
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})) == "deny"
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None


def test_a_different_search_after_a_denial_is_gated_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason this gate was rewritten. One nudge per session let an agent pay
    the toll once and then grep freely for the rest of the turn."""
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})) == "deny"
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "y"})) == "deny"
    assert decision(run(monkeypatch, capsys, repo, "Glob", {"pattern": "*.kt"})) == "deny"


def test_insisting_clears_only_that_one_search(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The escape hatch is per search, not a session-wide pass."""
    run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "z"})) == "deny"


def test_a_widened_result_window_is_the_same_search(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`head_limit` and `output_mode` do not change WHAT is searched, so raising
    one is the agent insisting, not asking something new."""
    run(monkeypatch, capsys, repo, "Grep", {"pattern": "x", "head_limit": 20})
    assert run(
        monkeypatch, capsys, repo, "Grep",
        {"pattern": "x", "head_limit": 200, "output_mode": "content"},
    ) is None


def test_a_new_session_is_gated_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}, session="s1")
    assert decision(
        run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"}, session="s2")
    ) == "deny"


def test_a_graph_query_buys_credit_for_the_searches_that_follow(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent that started at the graph follows up on the anchors it was
    handed, and must not be interrupted doing it."""
    assert run(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"q": "feed"}) is None
    for pattern in ("a", "b", "c"):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": pattern}) is None
    # Credit spent — the gate is live again.
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "d"})) == "deny"


def post(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proj: Path,
    tool: str,
    response: Any,
    session: str = "s1",
) -> None:
    """Drive the PostToolUse pass, where the graph's answer exists to be sized."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {},
        "tool_response": response,
        "cwd": str(proj),
        "session_id": session,
    }
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(payload)))
    gate.main()
    capsys.readouterr()


@pytest.mark.parametrize(("response", "expected"), [
    ({}, 3),                                                        # buffer only
    ({"anchors": ["a.py#F", "b.py#G"]}, 5),                         # 2 files + buffer
    ({"anchors": ["a.py#F", "a.py#G"]}, 4),                         # same file twice
    ({"results": [{"anchors": ["a.py"]}, {"anchors": ["b.py"]}]}, 5),  # nested
    ({"nodes": [{"anchors": [f"f{i}.py"]} for i in range(100)]}, 103),  # uncapped
    ("a string, not a structure", 3),
])
def test_an_answer_is_worth_the_files_it_named(
    response: Any, expected: int
) -> None:
    """One search per file the graph handed over, plus the buffer. An answer
    naming ten files is an agent with ten files to read."""
    assert gate.credit_for(response, {}) == expected


def test_the_grant_is_uncapped_because_the_count_is_evidence(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hundred uses across a hundred files is a hundred places to look. The
    anchor count is what the graph itself named, not a number this hook guessed,
    so there is nothing for a ceiling to protect against."""
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search",
         {"results": [{"anchors": [f"f{i}.py#S"]} for i in range(100)]})
    for i in range(103):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": f"p{i}"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "last"})) == "deny"


def test_following_up_on_anchored_files_costs_no_credit_at_all(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason the count rarely runs out in practice: a search that names an
    anchored file is never gated, so reading back what the graph handed over
    spends nothing. Credit is only for searches that are still hunting."""
    anchored = repo / "app" / "src" / "domain" / "FeedRanker.kt"
    anchored.write_text("class FeedRanker\n", encoding="utf-8")
    for _ in range(50):
        assert run(
            monkeypatch, capsys, repo, "Grep",
            {"pattern": "rank", "path": str(anchored)},
        ) is None
    # No credit was ever granted, and none was spent.
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "rank"})) == "deny"


def test_the_sized_grant_reaches_the_gate_end_to_end(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search",
         {"results": [{"anchors": ["a.py#A"]}, {"anchors": ["b.py#B"]}]})
    for pattern in "abcde":            # 2 anchors + 3 buffer
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": pattern}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "f"})) == "deny"


def test_a_post_pass_never_lowers_the_credit_already_held(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both passes fire around one call. The PostToolUse count must not take
    away the floor the PreToolUse pass granted for an answer it could not read."""
    run(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"q": "feed"})
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"error": "boom"})
    for pattern in "abc":
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": pattern}) is None


def test_asking_again_tops_the_credit_back_up(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole recovery path: run out, query again, carry on. Nothing about
    exhausting credit is terminal — it is a prompt to go back to the graph."""
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search",
         {"results": [{"anchors": ["a.py#A"]}]})          # 1 + 3 = 4
    for i in range(4):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": f"a{i}"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "spent"})) == "deny"

    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search",
         {"results": [{"anchors": [f"b{i}.py#B"]} for i in range(4)]})   # 4 + 3 = 7
    for i in range(7):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": f"b{i}"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "again"})) == "deny"


def test_credit_cannot_be_farmed_by_repeating_a_cheap_query(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A grant REPLACES rather than accumulates, which is why `max` and not `+`.
    Otherwise `kg_stats` in a loop banks the session: five answers naming nothing
    would buy fifteen searches for having learned nothing."""
    for _ in range(5):
        post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_stats", {})
    for i in range(3):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": f"c{i}"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "farmed"})) == "deny"


def test_a_smaller_answer_never_lowers_the_credit_in_hand(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of `max`: consulting the graph again must never cost an
    agent the allowance a bigger answer already earned it."""
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search",
         {"results": [{"anchors": [f"d{i}.py#D"]} for i in range(6)]})   # 9
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "one"}) is None  # 8 left
    post(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_stats", {})          # worth 3
    for i in range(8):
        assert run(monkeypatch, capsys, repo, "Grep", {"pattern": f"e{i}"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "gone"})) == "deny"


def test_gate_credit_is_configurable(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_local_config(repo, "gate_credit: 1")
    run(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"q": "feed"})
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "a"}) is None
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "b"})) == "deny"


def test_gate_credit_zero_means_a_query_buys_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The strictest setting still cannot strand anyone — the repeat works."""
    _write_local_config(repo, "gate_credit: 0")
    run(monkeypatch, capsys, repo, "mcp__codebase-kg__kg_search", {"q": "feed"})
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "a"})) == "deny"
    assert run(monkeypatch, capsys, repo, "Grep", {"pattern": "a"}) is None


@pytest.mark.parametrize(("raw", "expected"), [
    ({}, 3), ({"gate_credit": 5}, 5), ({"gate_credit": 0}, 0),
    ({"gate_credit": -1}, 3), ({"gate_credit": "lots"}, 3), ({"gate_credit": True}, 1),
])
def test_gate_credit_parsing(raw: dict[str, Any], expected: int) -> None:
    """A typo costs the setting, never the feature — the same posture as
    `nudge_every` and `search_gate`."""
    assert gate.gate_credit(raw) == expected


def test_a_search_scoped_to_an_anchored_file_is_never_gated(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The agent named the file. It has already answered the question the gate
    asks, so there is nothing to send it to the graph for."""
    anchored = repo / "app" / "src" / "domain" / "FeedRanker.kt"
    anchored.write_text("class FeedRanker\n", encoding="utf-8")
    assert run(
        monkeypatch, capsys, repo, "Grep", {"pattern": "rank", "path": str(anchored)}
    ) is None
    # And it spends no credit doing it.
    assert decision(run(monkeypatch, capsys, repo, "Grep", {"pattern": "rank"})) == "deny"


def test_a_directory_inside_root_is_still_gated(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory is where an agent looks when it does NOT know which file —
    exactly the case the gate exists for."""
    assert decision(run(
        monkeypatch, capsys, repo, "Grep",
        {"pattern": "x", "path": str(repo / "app" / "src" / "domain")},
    )) == "deny"


def test_an_unanchored_file_inside_root_is_still_gated(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = repo / "app" / "src" / "domain" / "Unmapped.kt"
    other.write_text("class Unmapped\n", encoding="utf-8")
    assert decision(run(
        monkeypatch, capsys, repo, "Grep", {"pattern": "x", "path": str(other)}
    )) == "deny"


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


def test_it_degrades_to_a_warning_when_state_cannot_be_written(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The escape hatch lives in the state file, so an unwritable temp dir means
    a repeat cannot be recognised — and a `deny` would then refuse the same
    search forever. It advises instead, which still says the thing and can never
    strand anyone."""
    def boom(*_: object, **__: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", boom)
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert decision(result) is None
    assert "kg_search" in result["systemMessage"]  # type: ignore[index]


def test_an_unreadable_graph_does_not_raise(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "knowledge" / "code_graph.db").write_bytes(b"not a database")
    result = run(monkeypatch, capsys, repo, "Grep", {"pattern": "x"})
    assert decision(result) == "deny"  # root unreadable → treated as the whole repo

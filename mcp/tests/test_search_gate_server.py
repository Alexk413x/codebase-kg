"""What the server adds to the search gate: memory for session state, and the credit count.

`test_search_gate.py` runs the gate's own rules against the hook script and against this endpoint.
This file covers the parts only the server has: it ties a model's graph query to a session through
the client id, it keeps sessions apart, and it says once when it started after a session did.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import classic, core, search_gate
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build


@pytest.fixture(autouse=True)
def fresh_server_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(search_gate, "_unusable", False)
    for store in (search_gate._states, search_gate._queried, search_gate._sessions):
        store.clear()
    yield
    for store in (search_gate._states, search_gate._queried, search_gate._sessions):
        store.clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = tmp_path / "repo"
    (proj / "knowledge").mkdir(parents=True)
    (proj / "app" / "src" / "domain").mkdir(parents=True)
    build(
        proj / "knowledge" / "code_graph.db",
        Meta(codebase="test", root="app/src", generated="2026-08-01"),
        [Node(id="feed_ranker", kind="Domain", description="Ranks the feed.",
              anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")], edges=[], section="DOMAIN")],
    )
    return proj


def _gate(repo: Path, client: str | None, session: str = "s1", pattern: str = "x", **more: Any) -> dict[str, Any]:
    result = search_gate.call({
        "tool_name": "Grep", "tool_input": json.dumps({"pattern": pattern}), "cwd": str(repo),
        "session_id": session, **more,
    }, client)
    assert result["isError"] is False and "structuredContent" not in result
    return json.loads(result["content"][0]["text"])


def _denied(out: dict[str, Any]) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_a_graph_query_clears_the_next_searches_of_the_session_that_made_it(repo: Path) -> None:
    assert _denied(_gate(repo, "client-a", pattern="p0"))
    search_gate.note_query("client-a")
    assert [_denied(_gate(repo, "client-a", pattern=f"p{i}")) for i in range(1, 5)] == [False, False, False, True]


def test_the_credit_is_the_grant_and_the_maximum(repo: Path) -> None:
    """A second query does not add to the count: it sets it to `gate_credit` again."""
    for _ in range(5):
        search_gate.note_query("client-a")
    assert [_denied(_gate(repo, "client-a", pattern=f"p{i}")) for i in range(4)] == [False, False, False, True]


def test_one_clients_query_earns_nothing_for_another_clients_session(repo: Path) -> None:
    search_gate.note_query("client-a")
    assert _denied(_gate(repo, "client-b", session="s-b"))
    assert not _denied(_gate(repo, "client-a", session="s-a"))


def test_a_query_from_a_client_the_gate_never_saw_is_kept_for_its_first_gate_call(repo: Path) -> None:
    """A model call carries no session id, so a query that comes before any hook call waits by client."""
    search_gate.note_query("client-new")
    assert "client-new" in search_gate._queried
    assert not _denied(_gate(repo, "client-new", session="later"))
    assert "client-new" not in search_gate._queried


def test_a_call_without_a_client_id_is_gated_and_earns_nothing(repo: Path) -> None:
    search_gate.note_query(None)
    search_gate.note_query("")
    assert not search_gate._queried
    assert _denied(_gate(repo, None))


def test_two_sessions_in_one_project_keep_their_own_state(repo: Path) -> None:
    assert _denied(_gate(repo, "c1", session="one", pattern="same"))
    assert _denied(_gate(repo, "c2", session="two", pattern="same")), "another session's denial is no repeat"
    assert not _denied(_gate(repo, "c1", session="one", pattern="same")), "a repeat in its own session passes"


def test_the_state_of_old_sessions_is_dropped_first(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_gate, "MAX_SESSIONS", 3)
    for i in range(5):
        _gate(repo, f"c{i}", session=f"s{i}")
    assert [session for _, session in search_gate._states] == ["s2", "s3", "s4"]


def test_the_tool_input_may_be_json_text_or_an_object(repo: Path) -> None:
    as_object = search_gate.call({"tool_name": "Grep", "tool_input": {"pattern": "y"}, "cwd": str(repo),
                                  "session_id": "obj"}, "c")
    assert _denied(json.loads(as_object["content"][0]["text"]))


@pytest.mark.parametrize("args", [None, "text", {}, {"tool_name": "Grep"}, {"tool_name": "Grep", "tool_input": "{broken",
                                                                          "cwd": "relative", "session_id": 7}])
def test_a_call_the_gate_cannot_read_answers_an_empty_object(args: Any) -> None:
    result = search_gate.call(args, "c")
    assert result["isError"] is False and json.loads(result["content"][0]["text"]) == {}


def test_a_failing_gate_answers_an_empty_object(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args: dict[str, Any], client: str | None = None) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(search_gate, "decide", boom)
    assert json.loads(search_gate.call({"tool_name": "Grep"}, "c")["content"][0]["text"]) == {}


def test_a_gate_script_without_a_part_the_server_replaces_turns_the_gate_off_and_logs_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The server swaps named functions of the script. If a rename removes one, the gate must not run
    with the script's own state files: it turns off, searches pass, and the log says why one time,
    not on every Bash call."""
    stub = tmp_path / "kg_search_gate.py"
    stub.write_text("def _run(data):\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(search_gate, "SCRIPT", stub)
    monkeypatch.setattr(search_gate, "_script", None)
    monkeypatch.setattr(search_gate, "_unusable", False)

    with caplog.at_level("ERROR", logger="codebase_kg.search_gate"):
        answers = [_gate(repo, "c", pattern=f"p{i}") for i in range(4)]

    assert answers == [{}, {}, {}, {}]
    assert len(caplog.records) == 1 and "could not be loaded" in caplog.records[0].getMessage()
    failure = caplog.records[0].exc_info
    assert failure is not None and "has no _read_state" in str(failure[1])


def test_loading_the_script_writes_no_bytecode_into_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = search_gate.SCRIPT.parent / "__pycache__"
    before = sorted(p.name for p in cache.iterdir()) if cache.is_dir() else None
    monkeypatch.setattr(search_gate, "_script", None)
    for name in ("kg_search_gate", "_config", "_shell_search", "_shim_loader"):
        monkeypatch.delitem(__import__("sys").modules, name, raising=False)
    search_gate._load()
    assert (sorted(p.name for p in cache.iterdir()) if cache.is_dir() else None) == before


# --- a server that started after the session ---------------------------------------------------------
FIRST_LINE = datetime(2026, 11, 25, 19, 17, 52, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(("stamp", "expected"), [
    ("2026-11-25T19:17:52.078Z", FIRST_LINE + 0.078), ("2026-11-25T19:17:52+00:00", FIRST_LINE),
    ("2026-11-25T20:17:52+01:00", FIRST_LINE),
])
def test_the_session_start_falls_back_to_the_first_line_where_no_creation_time_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str, expected: float,
) -> None:
    """Linux records no creation time, and `st_ctime` there moves with every appended line."""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "user", "timestamp": stamp}) + "\n" + json.dumps({"timestamp": "2030-01-01T00:00:00Z"}) + "\n",
                    encoding="utf-8")
    monkeypatch.setattr(search_gate, "_created", lambda p: None)
    assert search_gate.session_started(str(path)) == pytest.approx(expected)


def test_a_transcript_with_no_usable_start_gives_no_session_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_gate, "_created", lambda p: None)
    for body in ("", "not json\n", '{"timestamp": 5}\n', '{"no": "timestamp"}\n'):
        path = tmp_path / "t.jsonl"
        path.write_text(body, encoding="utf-8")
        assert search_gate.session_started(str(path)) is None
    assert search_gate.session_started(str(tmp_path / "missing.jsonl")) is None
    assert search_gate.session_started(None) is None


def test_the_notice_works_from_the_first_line_alone(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"timestamp": "2026-11-25T19:17:52Z"}\n', encoding="utf-8")
    monkeypatch.setattr(search_gate, "_created", lambda p: None)
    monkeypatch.setattr(search_gate, "STARTED", FIRST_LINE + search_gate.RESTART_SLACK + 30)
    assert _gate(repo, "c", transcript_path=str(path))["systemMessage"] == search_gate.RESTARTED
    monkeypatch.setattr(search_gate, "STARTED", FIRST_LINE + 5)
    assert "systemMessage" not in _gate(repo, "c2", session="young", transcript_path=str(path))


def test_the_creation_time_is_not_taken_from_ctime_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    class Stat:
        st_ctime = 123.0

    monkeypatch.setattr(os, "stat", lambda p: Stat())
    monkeypatch.setattr(sys, "platform", "linux")
    assert search_gate._created(str(path)) is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert search_gate._created(str(path)) == 123.0
    Stat.st_birthtime = 77.0  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert search_gate._created(str(path)) == 77.0


def _transcript(tmp_path: Path, age: float) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return str(path)


def test_a_session_older_than_the_server_is_told_once(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _transcript(tmp_path, 0)
    monkeypatch.setattr(search_gate, "STARTED", os.path.getctime(transcript) + search_gate.RESTART_SLACK + 30)

    first = _gate(repo, "c", transcript_path=transcript)
    again = _gate(repo, "c", pattern="other", transcript_path=transcript)

    assert _denied(first) and first["systemMessage"] == search_gate.RESTARTED
    assert "additionalContext" not in first["hookSpecificOutput"], "a deny carries its reason, not context"
    assert "systemMessage" not in again


def test_the_notice_rides_on_an_allowed_search_as_context_too(repo: Path, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _transcript(tmp_path, 0)
    monkeypatch.setattr(search_gate, "STARTED", os.path.getctime(transcript) + search_gate.RESTART_SLACK + 30)
    search_gate.note_query("c")

    out = _gate(repo, "c", transcript_path=transcript)

    assert out["systemMessage"] == search_gate.RESTARTED
    assert out["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "additionalContext": search_gate.RESTARTED}


def test_the_session_that_started_the_server_gets_no_notice(repo: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _transcript(tmp_path, 0)
    monkeypatch.setattr(search_gate, "STARTED", time.time() + 5)
    assert "systemMessage" not in _gate(repo, "c", transcript_path=transcript)
    assert "systemMessage" not in _gate(repo, "c2", session="no-transcript")
    assert "systemMessage" not in _gate(repo, "c3", session="gone", transcript_path=str(tmp_path / "missing.jsonl"))


def test_a_repo_without_a_graph_gets_no_notice_and_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    transcript = _transcript(tmp_path, 0)
    monkeypatch.setattr(search_gate, "STARTED", os.path.getctime(transcript) + 600)
    search_gate.note_query("c")
    assert _gate(bare, "c", transcript_path=transcript) == {}
    assert not search_gate._states


# --- the endpoint -----------------------------------------------------------------------------------------
def test_the_endpoint_is_not_in_the_catalog_and_every_path_reaches_it(repo: Path) -> None:
    assert search_gate.TOOL not in core.TOOLS
    args = {"tool_name": "Grep", "tool_input": json.dumps({"pattern": "z"}), "cwd": str(repo), "session_id": "e"}
    direct = core.hook_result(search_gate.TOOL, args, "c")
    assert direct is not None and _denied(json.loads(direct["content"][0]["text"]))
    reply = classic.answer(core.Core(None), None, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                   "params": {"name": search_gate.TOOL, "arguments": args}})
    assert reply is not None and json.loads(reply["result"]["content"][0]["text"]) == {}, "a repeat passes"

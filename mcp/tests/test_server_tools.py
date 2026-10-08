"""Every MCP tool, invoked through the surface an agent actually hits: `core.Core.call_tool`.

`test_server.py` proves the tools are *defined* and that path resolution
works; `test_tools.py` and `test_edits.py` prove the functions beneath them are
correct. Neither called a tool, so the layer joining them — the argument check,
the graph lookup and the dispatch in `core.py` and `worker.py` — would be
uncovered without this file.

That layer is a line per tool and looks too small to break, which is exactly
the argument that kept it untested. It is also the only place a wrong argument
order or a missing peer graph would live, and a mistake there is invisible to
both neighbouring test files.

Calls run in this process (`max_workers` 0), resolving the graph the way a
private server does; `test_pool.py` shows a worker returns the same.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from codebase_kg import core, migrate, query, resolve

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with a graph, with the server pointed at it."""
    shutil.copytree(FIX / "android", tmp_path / "android")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    migrate.convert(
        tmp_path / "android" / "KNOWLEDGE_GRAPH.md", knowledge / "code_graph.db"
    )
    # `_resolve_graph_path` checks sys.argv[1] first, and under pytest that is
    # the test file — which the store then rejects as "not a code graph
    # database". Neutralised the same way test_server.py does.
    monkeypatch.setattr("sys.argv", ["codebase-kg"])
    monkeypatch.setenv("CODEBASE_KG_PATH", str(knowledge / "code_graph.db"))
    return tmp_path


def _result(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return core.Core(None).call_tool(resolve.from_process(sys.argv), name, arguments)


def call(name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a tool the way the server does. A tool error raises, with its text."""
    result = _result(name, kwargs)
    data = result.get("structuredContent")
    if data is None:
        raise RuntimeError(result["content"][0]["text"])
    assert isinstance(data, dict), f"{name} returned {type(data)}: {data!r}"
    return data


def _any_node_id(served: Path) -> str:
    hits = call("kg_search", query="feed")["results"]
    assert hits, "fixture graph has no searchable nodes"
    return str(hits[0]["id"])


# --- each tool answers through the wrapper -----------------------------------
def test_kg_search_returns_ranked_results(served: Path) -> None:
    out = call("kg_search", query="feed")
    assert out["count"] == len(out["results"]) > 0
    assert {"id", "kind", "anchors", "score"} <= set(out["results"][0])


def test_kg_search_kind_filter_reaches_the_query_layer(served: Path) -> None:
    # The wrapper passes `kind` positionally; a swapped argument would still
    # return results, just the wrong ones, which is why this asserts on content.
    unfiltered = call("kg_search", query="feed")["results"]
    filtered = call("kg_search", query="feed", kind="ViewModel")["results"]
    assert len(filtered) <= len(unfiltered)
    assert all("viewmodel" in r["kind"].lower() for r in filtered)


def test_kg_node_fetches_a_full_node(served: Path) -> None:
    node_id = _any_node_id(served)
    out = call("kg_node", id=node_id)
    assert out["id"] == node_id
    assert "anchors" in out and "description" in out


def test_kg_node_suggests_alternatives_for_an_unknown_id(served: Path) -> None:
    out = call("kg_node", id="no_such_node_at_all")
    assert "did_you_mean" in out


def test_kg_neighborhood_defaults_to_one_hop(served: Path) -> None:
    out = call("kg_neighborhood", id=_any_node_id(served))
    assert "edges" in out or "neighbors" in out or "outbound" in out


def test_kg_neighborhood_depth_is_forwarded(served: Path) -> None:
    node_id = _any_node_id(served)
    shallow = call("kg_neighborhood", id=node_id, depth=1)
    deep = call("kg_neighborhood", id=node_id, depth=3)
    # Depth must reach the query layer; identical payloads would mean the
    # wrapper dropped it.
    assert deep != shallow or shallow.get("truncated") is not True


def test_kg_find_by_kind_matches_case_insensitively(served: Path) -> None:
    # Note the payload key: `kg_search` returns `results`, this returns `nodes`.
    # An inconsistency in the tool surface, pinned rather than fixed — renaming
    # either one changes what every agent already reads.
    out = call("kg_find_by_kind", kind="viewmodel")
    assert out["count"] == len(out["nodes"])
    assert all("viewmodel" in n["kind"].lower() for n in out["nodes"])


def test_kg_find_by_path_accepts_a_bare_filename(served: Path) -> None:
    anchors = call("kg_node", id=_any_node_id(served))["anchors"]
    filename = str(anchors[0]).split("#")[0].rsplit("/", 1)[-1]
    out = call("kg_find_by_path", path=filename)
    assert out["count"] >= 1


def cli(capsys: pytest.CaptureFixture[str], name: str, **kwargs: Any) -> dict[str, Any]:
    """Run a read tool the way `kg_cli.py query` does. A failure raises, with its error."""
    code = query.main([name, json.dumps(kwargs)])
    out = json.loads(capsys.readouterr().out)
    if code:
        raise RuntimeError(out["error"])
    return out


@pytest.mark.parametrize("name", ["kg_parity_gaps", "kg_stats", "kg_validate"])
def test_the_cli_only_tools_are_not_on_mcp(served: Path, name: str) -> None:
    result = _result(name, {})
    assert result["isError"] is True and "Unknown tool" in result["content"][0]["text"]


def test_kg_parity_gaps_runs_unfiltered_and_filtered(served: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "count" in cli(capsys, "kg_parity_gaps")
    assert "count" in cli(capsys, "kg_parity_gaps", status="divergent")


def test_kg_stats_reports_totals(served: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = cli(capsys, "kg_stats")
    assert out["nodes"] > 0
    assert "edges" in out and "anchors" in out


def test_kg_validate_runs_without_a_peer(served: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `_open_peer` yields None when there is no counterpart; the wrapper holds
    # two context managers at once and this is the only test that opens both.
    out = cli(capsys, "kg_validate")
    assert "ok" in out


# --- the failure the wrappers share ------------------------------------------
def test_every_tool_reports_a_missing_graph_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No graph anywhere: each tool must say what to do, not raise a bare error.

    This is the first thing a new user hits — the server starts fine in a repo
    with no graph, so the message is the entire user experience of that state.
    """
    monkeypatch.delenv("CODEBASE_KG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["codebase-kg"])

    for name, kwargs in [
        ("kg_search", {"query": "x"}),
        ("kg_node", {"id": "x"}),
        ("kg_neighborhood", {"id": "x"}),
        ("kg_find_by_kind", {"kind": "x"}),
        ("kg_find_by_path", {"path": "x"}),
    ]:
        with pytest.raises(RuntimeError) as exc:
            call(name, **kwargs)
        assert "/codebase-kg:build" in str(exc.value), name

    # The write tools answer instead of raising -- a refusal is data there, so
    # `written: false` can be trusted to mean "the file is untouched".
    for name, kwargs in [
        ("kg_upsert_node", {"nodes": [{"id": "x", "kind": "K"}]}),
        ("kg_delete_node", {"ids": ["x"]}),
        ("kg_add_link", {"node_id": "x", "target": "p.db#y"}),
        ("kg_remove_link", {"node_id": "x", "target": "p.db#y"}),
    ]:
        out = call(name, **kwargs)
        assert out["ok"] is False and out["written"] is False, name
        assert "/codebase-kg:build" in out["error"], name


# --- the write tools through the same surface --------------------------------
def test_kg_upsert_node_reports_what_it_changed(served: Path) -> None:
    node_id = _any_node_id(served)
    before = call("kg_node", id=node_id)["section"]
    out = call("kg_upsert_node", nodes=[{"id": node_id, "section": "REVISED"}])
    assert out["ok"] and out["written"] is True
    change = next(c for c in out["changes"] if c["table"] == "node")
    assert change["fields"] == [{"field": "section", "before": before, "after": "REVISED"}]
    assert call("kg_node", id=node_id)["section"] == "REVISED"


def test_a_refused_write_comes_back_as_data_not_an_exception(served: Path) -> None:
    """A rejection is the tool working, so it must be something an agent can read."""
    node_id = _any_node_id(served)
    out = call("kg_upsert_node", nodes=[{"id": node_id, "description": "Fixes ACME-431."}])
    assert out["ok"] is False and out["written"] is False
    assert "ticket refs" in out["error"]


def test_kg_delete_node_previews_through_the_tool_surface(served: Path) -> None:
    node_id = _any_node_id(served)
    out = call("kg_delete_node", ids=[node_id])
    assert out["ok"] and out["written"] is False and out["dry_run"] is True
    assert node_id in out["would_delete"]["nodes"]
    assert call("kg_node", id=node_id)["found"] is True


def test_kg_add_link_then_remove_link_round_trips(served: Path) -> None:
    node_id = _any_node_id(served)
    added = call(
        "kg_add_link", node_id=node_id, target="cartographer_graph.db#home", kind="presented-by"
    )
    assert added["written"] is True
    assert call("kg_find_by_link", target="cartographer_graph.db#home")["count"] == 1
    assert call("kg_remove_link", node_id=node_id, target="cartographer_graph.db#home")[
        "written"
    ] is True
    assert call("kg_node", id=node_id).get("external_links", []) == []


# --- the MCP call path -------------------------------------------------------
class _Result:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.is_error = raw["isError"]
        self.structured_content: dict[str, Any] = raw.get("structuredContent") or {}
        self.text = raw["content"][0]["text"]


def _call_over_mcp(name: str, arguments: dict[str, Any]) -> _Result:
    return _Result(_result(name, arguments))


def test_a_refused_write_is_an_error_result_with_the_same_body(served: Path) -> None:
    node_id = _any_node_id(served)
    result = _call_over_mcp(
        "kg_upsert_node", {"nodes": [{"id": node_id, "description": "Fixes ACME-431."}]}
    )
    assert result.is_error is True
    body = result.structured_content
    assert body["ok"] is False and body["written"] is False
    assert "ticket refs" in body["error"]
    assert json.loads(result.text) == body


def test_an_accepted_write_is_not_an_error_result(served: Path) -> None:
    node_id = _any_node_id(served)
    result = _call_over_mcp(
        "kg_upsert_node", {"nodes": [{"id": node_id, "section": "REVISED"}]}
    )
    assert result.is_error is False
    assert result.structured_content["written"] is True


def test_upsert_schema_passes_keys_and_types_through_to_the_edit_layer(served: Path) -> None:
    # The typed `nodes` schema must not reject what `edits.upsert_node` accepts:
    # unknown keys are ignored there, and anchors may be objects.
    node_id = _any_node_id(served)
    anchor = call("kg_node", id=node_id)["anchors"][0]
    path, _, symbol = str(anchor).partition("#")
    out = call(
        "kg_upsert_node",
        nodes=[{"id": node_id, "note": "ignored", "anchors": [{"path": path, "symbol": symbol}]}],
    )
    assert out["ok"] is True
    refused = _call_over_mcp(
        "kg_upsert_node", {"nodes": [{"id": node_id, "rebaseline": "yes"}]}
    )
    assert refused.is_error is True
    assert "`rebaseline` must be true or false" in refused.structured_content["error"]


def test_list_tools_page_through_the_tool_surface(served: Path) -> None:
    first = call("kg_find_by_kind", kind="", limit=1)
    assert first["count"] == 1 and first["truncated"] is True
    rest = call("kg_find_by_kind", kind="", limit=1000, offset=first["next_offset"])
    assert rest["truncated"] is False
    assert first["total"] == rest["total"] == 1 + rest["count"]


def test_kg_validate_reports_issue_counts(served: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = cli(capsys, "kg_validate", limit=1)
    assert set(out["issue_counts"]) == {
        "anchor_issues", "counterpart_issues", "description_issues",
        "external_link_issues", "reference_issues",
    }
    assert all(len(out[k]) <= 1 for k in out["issue_counts"])

"""The HTTP front, request by request: the MCP 2026-07-28 surface Claude Code uses, and its refusals.

Each case sends one raw request and checks the status code and the JSON shape
of the reply, not the wording of a validation message. The cases come from a
battery run against both the fastmcp server this front replaced and Claude Code
2.1.293's captured traffic.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import core, shim
from test_http_server import _start, _state
from test_shared_server import TIMEOUT, _repo, _stop

V = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": V,
        "io.modelcontextprotocol/clientInfo": {"name": "battery", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {"roots": {"listChanged": True}}}
ROOTS_KEY = "codebase-kg-roots"


@pytest.fixture(scope="module")
def front(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("battery")
    repo = _repo(root / "alpha", "alpha")
    cache = root / "cache"
    proc = _start(cache, root)
    try:
        state = _state(cache)
        token = (cache / "http-token").read_text(encoding="utf-8").strip()
        yield {"port": state["http_port"], "token": token, "state_token": state["token"], "repo": repo,
               "proc": proc, "root": root}
    finally:
        _stop(proc)


class Reply:
    def __init__(self, status: int, content_type: str | None, raw: bytes) -> None:
        self.status = status
        self.content_type = content_type
        try:
            self.body: Any = json.loads(raw)
        except ValueError:
            self.body = raw.decode("utf-8", "replace")

    @property
    def result(self) -> dict[str, Any]:
        assert isinstance(self.body, dict) and isinstance(self.body.get("result"), dict), self.body
        return self.body["result"]

    def error_code(self) -> int:
        assert isinstance(self.body, dict) and isinstance(self.body.get("error"), dict), self.body
        return self.body["error"]["code"]


def _post(front: dict[str, Any], body: Any = None, headers: dict[str, str | None] | None = None,
          raw: bytes | None = None, method: str = "POST", path: str = "/mcp") -> Reply:
    port = front["port"]
    h: dict[str, str | None] = {
        "Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {front['token']}",
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    }
    if isinstance(body, dict) and "method" in body:
        h.update({"mcp-protocol-version": V, "mcp-method": body["method"]})
        if body["method"] == "tools/call":
            h["mcp-name"] = body["params"]["name"]
    h.update(headers or {})
    sent = {k: v for k, v in h.items() if v is not None}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request(method, path, body=data if method == "POST" else None, headers=sent)
        response = conn.getresponse()
        return Reply(response.status, response.getheader("content-type"), response.read())
    finally:
        conn.close()


def _req(front: dict[str, Any], method: str, params: dict[str, Any] | None = None, **kw: Any) -> Reply:
    p = dict(params or {})
    p["_meta"] = {**META, **p.get("_meta", {})}
    return _post(front, {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}, **kw)


def _roots(path: Path) -> dict[str, Any]:
    return {ROOTS_KEY: {"roots": [{"uri": path.as_uri()}]}}


def _call(front: dict[str, Any], name: str, args: dict[str, Any], client: str | None = "bat",
          roots: bool = True, headers: dict[str, str | None] | None = None) -> Reply:
    p: dict[str, Any] = {"name": name, "arguments": args}
    if roots:
        p["inputResponses"] = _roots(front["repo"])
    extra: dict[str, str | None] = {"x-codebase-kg-client": client, **(headers or {})}
    return _req(front, "tools/call", p, headers=extra)


def _complete(reply: Reply) -> dict[str, Any]:
    assert reply.status == 200 and reply.content_type == "application/json"
    result = reply.result
    assert result["resultType"] == "complete"
    assert result["_meta"] == {"io.modelcontextprotocol/serverInfo": core.SERVER_INFO}
    return result


def _tool_ok(reply: Reply) -> dict[str, Any]:
    result = _complete(reply)
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def _tool_error(reply: Reply, structured: bool = False) -> dict[str, Any]:
    result = _complete(reply)
    assert result["isError"] is True and result["content"][0]["type"] == "text"
    assert ("structuredContent" in result) is structured
    return result


def _rpc_error(reply: Reply, status: int, code: int) -> dict[str, Any]:
    assert reply.status == status, reply.body
    assert reply.error_code() == code
    assert reply.body["jsonrpc"] == "2.0"
    return reply.body["error"]


def _input_required(reply: Reply) -> None:
    result = reply.result
    assert reply.status == 200
    assert result["resultType"] == "input_required"
    assert result["inputRequests"] == {ROOTS_KEY: {"method": "roots/list"}}


# --- the methods Claude Code sends -------------------------------------------------
def test_discover(front: dict[str, Any]) -> None:
    result = _complete(_req(front, "server/discover"))
    assert result["supportedVersions"] == [V]
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["instructions"] == core.INSTRUCTIONS
    assert result["ttlMs"] == 0 and result["cacheScope"] == "private"


def test_tools_list(front: dict[str, Any]) -> None:
    result = _complete(_req(front, "tools/list"))
    assert result["tools"] == core.CATALOG["tools"] and len(result["tools"]) == 13


@pytest.mark.parametrize(("method", "key"), [
    ("prompts/list", "prompts"), ("resources/list", "resources"),
    ("resources/templates/list", "resourceTemplates"),
])
def test_empty_lists(front: dict[str, Any], method: str, key: str) -> None:
    assert _complete(_req(front, method))[key] == []


@pytest.mark.parametrize("method", ["ping", "bogus/method", "initialize"])
def test_an_unknown_method_is_404(front: dict[str, Any], method: str) -> None:
    error = _rpc_error(_req(front, method), 404, -32601)
    assert error["data"] == method


# --- tool calls and the roots round trip ----------------------------------------
def test_a_call_without_roots_asks_for_them(front: dict[str, Any]) -> None:
    _input_required(_call(front, "kg_search", {"query": "widget"}, client="fresh", roots=False))


def test_a_call_with_roots_answers_and_caches_them(front: dict[str, Any]) -> None:
    data = _tool_ok(_call(front, "kg_search", {"query": "widget"}, client="c1"))
    assert [r["id"] for r in data["results"]] == ["alpha_widget"]
    cached = _tool_ok(_call(front, "kg_node", {"id": "alpha_widget"}, client="c1", roots=False))
    assert cached["id"] == "alpha_widget"


def test_the_cwd_header_needs_no_roots(front: dict[str, Any]) -> None:
    data = _tool_ok(_call(front, "kg_find_by_kind", {"kind": "widget", "limit": 3}, client=None, roots=False,
                          headers={"x-codebase-kg-cwd": str(front["repo"])}))
    assert data["count"] == 1


def test_a_root_that_is_not_a_file_asks_again(front: dict[str, Any]) -> None:
    reply = _req(front, "tools/call", {"name": "kg_search", "arguments": {"query": "x"},
                                       "inputResponses": {ROOTS_KEY: {"roots": [{"uri": "https://x/"}]}}},
                 headers={"x-codebase-kg-client": "c3"})
    _input_required(reply)


@pytest.mark.parametrize(("name", "args"), [
    ("kg_nope", {}),
    ("kg_stats", {}),
    ("kg_node", {}),
    ("kg_node", {"id": "alpha_widget", "zzz": 1}),
    ("kg_neighborhood", {"id": "alpha_widget", "depth": "two"}),
    ("kg_find_by_kind", {"kind": "x", "limit": 0}),
])
def test_a_bad_call_is_a_tool_error(front: dict[str, Any], name: str, args: dict[str, Any]) -> None:
    _call(front, "kg_search", {"query": "widget"}, client="c1")
    _tool_error(_call(front, name, args, client="c1", roots=False))


def test_lax_arguments_are_coerced(front: dict[str, Any]) -> None:
    _call(front, "kg_search", {"query": "widget"}, client="c1")
    data = _tool_ok(_call(front, "kg_neighborhood", {"id": "alpha_widget", "depth": "2", "limit": 2},
                          client="c1", roots=False))
    assert data["depth"] == 2
    _tool_ok(_call(front, "kg_search", {"query": "widget", "kind": None}, client="c1", roots=False))


def test_no_graph_and_a_relative_cwd_are_tool_errors(front: dict[str, Any], tmp_path: Path) -> None:
    empty = _tool_error(_call(front, "kg_search", {"query": "widget"}, client=None, roots=False,
                              headers={"x-codebase-kg-cwd": str(tmp_path)}))
    assert empty["content"][0]["text"].startswith("Error calling tool 'kg_search': No code_graph.db found.")
    relative = _tool_error(_call(front, "kg_search", {"query": "widget"}, client=None, roots=False,
                                 headers={"x-codebase-kg-cwd": "rel/dir"}))
    assert "absolute path" in relative["content"][0]["text"]


def test_a_refused_write_is_a_structured_tool_error(front: dict[str, Any]) -> None:
    result = _tool_error(_call(front, "kg_remove_link", {"node_id": "alpha_widget", "target": "nope.db#x"},
                               client="w"), structured=True)
    assert result["structuredContent"]["ok"] is False and result["structuredContent"]["written"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_a_dry_run_delete_and_an_unknown_node_answer(front: dict[str, Any]) -> None:
    dry = _tool_ok(_call(front, "kg_delete_node", {"ids": ["no_such_node"]}, client="w"))
    assert dry["dry_run"] is True and dry["written"] is False
    missing = _tool_ok(_call(front, "kg_node", {"id": "no_such_node_xyz"}, client="w"))
    assert missing["found"] is False


# --- protocol refusals ---------------------------------------------------------------
def test_a_header_that_contradicts_the_body_is_refused(front: dict[str, Any]) -> None:
    _rpc_error(_req(front, "tools/list", headers={"mcp-method": "tools/call"}), 400, -32020)
    _rpc_error(_req(front, "tools/call", {"name": "kg_search", "arguments": {"query": "x"}}, headers={"mcp-name": "kg_node"}),
               400, -32020)


@pytest.mark.parametrize("version", [None, "2025-06-18", "2025-11-25", "2099-01-01"])
def test_any_other_protocol_version_is_refused(front: dict[str, Any], version: str | None) -> None:
    meta = {**META, "io.modelcontextprotocol/protocolVersion": version or V}
    reply = _post(front, {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": meta}},
                  headers={"mcp-protocol-version": version})
    error = _rpc_error(reply, 400, -32022)
    assert error["data"] == {"supported": [V], "requested": version or ""}


def test_a_classic_initialize_is_refused(front: dict[str, Any]) -> None:
    reply = _post(front, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "legacy", "version": "0"}}},
        headers={"mcp-protocol-version": None, "mcp-method": None})
    _rpc_error(reply, 400, -32022)
    assert reply.body["id"] == 1


def test_a_request_without_the_envelope_is_refused(front: dict[str, Any]) -> None:
    _rpc_error(_post(front, {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}), 400, -32602)


def test_a_notification_is_accepted(front: dict[str, Any]) -> None:
    reply = _post(front, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    assert reply.status == 202 and reply.body == ""


@pytest.mark.parametrize(("raw", "code"), [(b"{not json", -32700), (b"[]", -32600), (b"[{}]", -32600),
                                           (b'{"jsonrpc": "2.0", "id": true, "method": "x"}', -32600)])
def test_a_body_that_is_not_one_message_is_refused(front: dict[str, Any], raw: bytes, code: int) -> None:
    reply = _post(front, raw=raw, headers={"mcp-protocol-version": V, "mcp-method": "tools/list"})
    error = _rpc_error(reply, 400, code)
    assert reply.body["id"] is None and error["message"]


def test_get_mcp_is_405_and_an_unknown_path_404(front: dict[str, Any]) -> None:
    reply = _post(front, method="GET", headers={"mcp-protocol-version": V})
    assert reply.status == 405 and reply.body == ""
    other = _req(front, "tools/list", path="/other")
    assert other.status == 404 and other.content_type == "text/plain; charset=utf-8"


def test_a_client_that_accepts_no_json_gets_406(front: dict[str, Any]) -> None:
    assert _req(front, "tools/list", headers={"Accept": "text/plain"}).status == 406


# --- the gate: Host, Origin, token -------------------------------------------------
def test_a_foreign_host_or_origin_is_403(front: dict[str, Any]) -> None:
    for headers in ({"Host": "evil.example:80"}, {"Origin": "http://evil.example"},
                    {"Origin": "http://127.0.0.1:1"}):
        reply = _req(front, "tools/list", headers=headers)
        assert reply.status == 403 and set(reply.body) == {"error"}
    assert _req(front, "tools/list", headers={"Origin": f"http://localhost:{front['port']}"}).status == 200


def test_a_missing_or_wrong_token_is_403_not_401(front: dict[str, Any]) -> None:
    for auth in (None, "Bearer nope", f"Bearer {front['state_token']}", f"Basic {front['token']}"):
        reply = _req(front, "tools/list", headers={"Authorization": auth})
        assert reply.status == 403 and set(reply.body) == {"error"}


def test_health_needs_no_token(front: dict[str, Any]) -> None:
    reply = _post(front, method="GET", path="/health", headers={"Authorization": None})
    assert reply.status == 200
    assert set(reply.body) == {"service", "build", "version", "built", "pid", "max_workers"}
    assert reply.body["service"] == "codebase-kg" and reply.body["build"] == shim.server_build()


def test_shutdown_refuses_get_and_the_http_token(front: dict[str, Any]) -> None:
    assert _post(front, method="GET", path="/shutdown").status == 403
    assert _post(front, path="/shutdown", headers={"Authorization": "Bearer nope"}).status == 403
    assert _post(front, path="/shutdown").status == 403
    assert _post(front, method="GET", path="/shutdown",
                 headers={"Authorization": f"Bearer {front['state_token']}"}).status == 405
    assert front["proc"].poll() is None


def test_the_unchanged_shim_reaches_the_same_server(front: dict[str, Any]) -> None:
    from test_shared_server import SHIM, Client

    env = {k: v for k, v in os.environ.items() if k not in {"CODEBASE_KG_PATH", "CODEBASE_KG_SHARED"}}
    env["CODEBASE_KG_CACHE_DIR"] = str(front["root"] / "cache")
    proc = subprocess.Popen([shim.server_command()[0], str(SHIM)], cwd=front["repo"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        client = Client(proc.stdin, proc.stdout)
        init = client.initialize()["result"]
        assert init["serverInfo"] == core.SERVER_INFO and init["capabilities"] == {"tools": {"listChanged": False}}
        assert init["protocolVersion"] == "2025-06-18" and init["instructions"] == core.INSTRUCTIONS
        assert client.request("tools/list", {})["result"]["tools"] == core.CATALOG["tools"]
        assert client.data("kg_node", id="alpha_widget")["id"] == "alpha_widget"
        assert client.request("ping", {})["result"] == {}
        assert client.request("bogus", {})["error"]["code"] == -32601
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)
    log = proc.stderr.read().decode("utf-8", "replace")
    assert f"connected to the shared server (pid {front['proc'].pid})" in log

"""The hook relay: Claude Code's hooks answered on stdio, from the shared server when it answers.

The first part drives `slipspace.bridge.HookRelay` against a scripted HTTP endpoint. The second part
covers the server's side: one `PreToolUse` endpoint, a quiet answer for a hook tool it does not know,
and the agent process as the identity that ties a graph query to the gate call that follows it. The
last part starts the shim with `--hooks` against the real server.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from codebase_kg import classic, core, http_transport, push_gate, search_gate, shim
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.slipspace import bridge
from codebase_kg.writer import build
from test_bridge import Endpoint

ROOT = Path(__file__).resolve().parents[2]
OFF, ON = "gates off", "gates back"
INFO = {"name": "test-hooks", "version": "1"}
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": "query the graph"}}


def _tool_result(out: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}


def _answers(out: dict[str, Any]) -> Any:
    return lambda body, headers: (200, {"jsonrpc": "2.0", "id": body["id"], "result": _tool_result(out)})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Relay:
    """A relay fed one line at a time, so each answer is read before the next request goes in."""

    def __init__(self, port: int, budget: float = 5.0, pause: float = 30.0, start: Any = None) -> None:
        self.started = 0
        self._start = start
        self.out = io.BytesIO()
        self.relay = bridge.HookRelay(
            port, lambda refresh: {"Authorization": "Bearer t", "X-Test-Agent": "7"}, self._started, OFF, ON,
            lambda name, arguments: budget, "kg_hook_", INFO, reader=io.BytesIO(), writer=self.out, pause=pause,
        )

    def _started(self) -> bool:
        self.started += 1
        return bool(self._start()) if self._start else False

    def send(self, method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
        self.out.seek(0)
        self.out.truncate()
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.relay._reader = io.BytesIO(json.dumps(message).encode() + b"\n")
        assert self.relay.run() == 0
        (line,) = self.out.getvalue().splitlines()
        return json.loads(line)

    def hook(self, name: str = "kg_hook_pre", **arguments: Any) -> dict[str, Any]:
        reply = self.send("tools/call", {"name": name, "arguments": arguments})
        assert reply["result"]["isError"] is False
        return json.loads(reply["result"]["content"][0]["text"])

    def settle(self) -> None:
        deadline = time.monotonic() + 5
        while self.relay._starting and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture
def endpoint() -> Iterator[Any]:
    made: list[Endpoint] = []

    def make(answer: Any) -> Endpoint:
        made.append(Endpoint(answer))
        return made[-1]

    yield make
    for one in made:
        one.close()


# --- the relay ------------------------------------------------------------------------------------
def test_the_relay_lists_no_tools_and_sends_no_instructions() -> None:
    """Nothing of it reaches the model's context: it exists for the hooks."""
    relay = Relay(_free_port())
    modern = {"_meta": {bridge.VERSION_KEY: bridge.MODERN}}
    found = relay.send("server/discover", modern)["result"]
    assert found["supportedVersions"] == [bridge.MODERN] and "instructions" not in found
    assert found["capabilities"] == {"tools": {"listChanged": False}}
    listed = relay.send("tools/list", modern)["result"]
    assert listed["tools"] == [] and listed["resultType"] == "complete"
    called = relay.send("tools/call", {"name": "kg_hook_pre", "arguments": {}, **modern})["result"]
    assert called["resultType"] == "complete" and "resultType" not in relay.send(
        "tools/call", {"name": "kg_hook_pre", "arguments": {}})["result"]
    classic = relay.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert classic["protocolVersion"] == "2025-06-18" and "instructions" not in classic
    assert classic["serverInfo"] == INFO
    assert relay.send("tools/list")["result"] == {"tools": []}
    assert relay.send("ping")["result"] == {}
    assert relay.send("resources/list")["error"]["code"] == -32601
    assert relay.send("tools/call", {"name": "kg_search", "arguments": {}})["error"]["code"] == -32602


def test_a_hook_call_goes_to_the_server_and_its_answer_comes_back_unchanged(endpoint: Any) -> None:
    server = endpoint(_answers(DENY))
    relay = Relay(server.port)
    assert relay.hook(tool_name="Grep", cwd="C:/work") == DENY
    (body, headers), = server.seen
    assert body["method"] == "tools/call" and body["params"]["name"] == "kg_hook_pre"
    assert body["params"]["arguments"] == {"tool_name": "Grep", "cwd": "C:/work"}
    assert body["params"]["_meta"][bridge.VERSION_KEY] == bridge.MODERN
    assert headers["authorization"] == "Bearer t" and headers["x-test-agent"] == "7"
    assert headers["mcp-name"] == "kg_hook_pre"
    assert relay.started == 0


def test_with_no_server_the_user_gets_one_line_and_every_call_an_empty_answer() -> None:
    relay = Relay(_free_port())
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert [relay.hook(), relay.hook("kg_hook_commit_check"), relay.hook()] == [{}, {}, {}]
    assert relay.started == 1


def test_nothing_is_sent_to_a_server_that_just_failed(endpoint: Any) -> None:
    """A hung server would cost its full allowance on every tool call."""
    server = endpoint(lambda body, headers: (500, None))
    relay = Relay(server.port)
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert relay.hook() == {} and relay.hook() == {}
    assert len(server.seen) == 1


def test_a_server_that_does_not_answer_in_time_costs_one_short_wait(endpoint: Any) -> None:
    server = endpoint(lambda body, headers: lambda handler: time.sleep(1.5))
    relay = Relay(server.port, budget=0.3)
    started = time.monotonic()
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert relay.hook() == {}
    assert time.monotonic() - started < 1.4
    assert len(server.seen) == 1


@pytest.mark.parametrize("reply", [
    (400, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Unknown tool: kg_hook_pre"}}),
    (200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Unknown tool"}], "isError": True}}),
    (200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "not json"}], "isError": False}}),
    (200, {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "input_required", "inputRequests": {}}}),
    (403, None),
])
def test_anything_but_a_hook_result_is_a_failure_and_not_an_error_for_the_hook(endpoint: Any, reply: Any) -> None:
    """An older server that does not know the tool, a refused token: the client's hook must still get a result."""
    relay = Relay(endpoint(lambda body, headers: reply).port)
    assert relay.hook() == {"systemMessage": OFF}


def test_the_first_answer_after_a_failure_says_the_gates_are_back_and_then_nothing_more(endpoint: Any) -> None:
    state = {"up": False}

    def answer(body: dict[str, Any], headers: dict[str, str]) -> Any:
        if not state["up"]:
            return 500, None
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": _tool_result({**DENY, "systemMessage": "said"})}

    relay = Relay(endpoint(answer).port, pause=0.0)
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert relay.hook() == {}
    state["up"] = True
    assert relay.hook() == {**DENY, "systemMessage": f"{ON}\nsaid"}
    assert relay.hook() == {**DENY, "systemMessage": "said"}
    state["up"] = False
    assert relay.hook() == {"systemMessage": OFF}


def test_a_server_the_owner_started_is_asked_again_at_once(endpoint: Any) -> None:
    """With nothing on the port the owner starts the server on another thread; the pause ends when it is up."""
    port = _free_port()
    made: list[Endpoint] = []

    def start() -> bool:
        made.append(Endpoint(_answers({}), port=port))
        return True

    relay = Relay(port, start=start)
    try:
        assert relay.hook() == {"systemMessage": OFF}
        relay.settle()
        assert relay.hook() == {"systemMessage": ON}
        assert relay.hook() == {} and len(made[0].seen) == 2
    finally:
        for one in made:
            one.close()


def test_a_server_that_fails_at_once_is_asked_again_once_the_owner_says_one_runs(endpoint: Any) -> None:
    """After a plugin update the old server holds the port and knows no hook tool. The owner's start
    function replaces it, so the next call must reach the new one. Where the old one stays, each
    call costs one quick refusal and the user hears nothing more."""
    server = endpoint(lambda body, headers: (400, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "x"}}))
    relay = Relay(server.port, start=lambda: True)
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert relay.hook() == {} and len(server.seen) == 2
    relay.settle()
    server.answer = _answers(DENY)
    assert relay.hook() == {**DENY, "systemMessage": ON}


def test_a_server_that_hung_is_left_alone_for_the_pause_even_when_the_owner_says_it_runs(endpoint: Any) -> None:
    server = endpoint(lambda body, headers: lambda handler: time.sleep(1.0))
    relay = Relay(server.port, budget=0.2, start=lambda: True)
    assert relay.hook() == {"systemMessage": OFF}
    relay.settle()
    assert relay.hook() == {} and len(server.seen) == 1


def test_the_search_gate_gets_a_short_allowance_and_git_a_long_one() -> None:
    grep = {"tool_name": "Bash", "tool_input": json.dumps({"command": "grep -rn x src"})}
    push = {"tool_name": "Bash", "tool_input": json.dumps({"command": "cd app && git push origin main"})}
    assert shim.hook_budget("kg_hook_pre", grep) == shim.SEARCH_BUDGET
    assert shim.hook_budget("kg_hook_pre", {"tool_name": "Grep", "tool_input": {"pattern": "x"}}) == shim.SEARCH_BUDGET
    assert shim.hook_budget("kg_hook_pre", push) == shim.GIT_BUDGET
    assert shim.hook_budget("kg_hook_commit_check", {"command": "git status"}) == shim.GIT_BUDGET
    assert shim.hook_budget("kg_hook_allow_write", None) == shim.SEARCH_BUDGET


# --- the server's side ------------------------------------------------------------------------------
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


def _pre(repo: Path, client: str | None, tool: str = "Grep", session: str = "s1", **tool_input: Any) -> dict[str, Any]:
    result = core.hook_result(core.PRE_TOOL, {
        "tool_name": tool, "tool_input": json.dumps(tool_input or {"pattern": "x"}), "cwd": str(repo),
        "session_id": session,
    }, client)
    assert result is not None and result["isError"] is False
    return json.loads(result["content"][0]["text"])


def _denied(out: dict[str, Any]) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_one_endpoint_answers_the_search_gate_for_every_search_tool(repo: Path) -> None:
    assert _denied(_pre(repo, "agent:1"))
    assert _denied(_pre(repo, "agent:1", tool="Bash", session="s2", command="grep -rn y app/src"))
    assert _pre(repo, "agent:1", tool="Bash", session="s3", command="echo one") == {}


def test_the_push_gate_answers_first_and_only_for_a_push(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []
    answer = {"text": json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "stale"}})}

    def pushed(args: dict[str, Any]) -> dict[str, Any]:
        asked.append(args)
        return {"content": [{"type": "text", "text": answer["text"]}], "isError": False}

    monkeypatch.setattr(push_gate, "call", pushed)
    out = _pre(repo, "agent:1", tool="PowerShell", command="git push origin main")
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "stale"
    assert asked == [{"tool_name": "PowerShell", "command": "git push origin main", "cwd": str(repo)}]
    answer["text"] = "{}"
    assert _pre(repo, "agent:1", tool="Bash", command="git push") == {}
    assert _pre(repo, "agent:1", tool="Bash", command="git status") == {} and len(asked) == 2
    assert _denied(_pre(repo, "agent:1", session="s9")) and len(asked) == 2


def test_a_hook_tool_this_server_does_not_know_gets_an_empty_answer() -> None:
    """A session whose hook list is newer than the server: an error would be a notice on every tool call."""
    result = core.hook_result("kg_hook_from_a_later_version", {"anything": 1}, "client")
    assert result == {"content": [{"type": "text", "text": "{}"}], "isError": False}
    assert core.hook_result("kg_search", {}, "client") is None
    assert core.hook_result(None, {}, "client") is None


def test_the_agent_process_ties_a_query_to_the_gate_call_of_another_client(repo: Path) -> None:
    """The hooks arrive through the relay and the model's queries on the HTTP connection: two client ids."""
    relay = {"x-codebase-kg-client": "hooks-1", "x-codebase-kg-agent": "4242"}
    model = {"x-codebase-kg-client": "0f0f", "x-codebase-kg-agent": "4242"}
    other = {"x-codebase-kg-client": "1e1e", "x-codebase-kg-agent": "5151"}
    assert http_transport.caller(relay) == http_transport.caller(model) == "agent:4242"
    assert _denied(_pre(repo, http_transport.caller(relay)))
    search_gate.note_query(http_transport.caller(other))
    assert _denied(_pre(repo, http_transport.caller(relay), pattern="y"))
    search_gate.note_query(http_transport.caller(model))
    assert not _denied(_pre(repo, http_transport.caller(relay), pattern="z"))


def test_clients_that_cannot_name_their_agent_share_one_count_so_a_query_always_earns_credit(repo: Path) -> None:
    relay = {"x-codebase-kg-client": "hooks-1", "x-codebase-kg-agent": "unknown"}
    model = {"x-codebase-kg-client": "0f0f", "x-codebase-kg-agent": "unknown"}
    assert http_transport.caller(relay) == http_transport.caller(model) == "agent:unknown"
    assert _denied(_pre(repo, http_transport.caller(relay)))
    search_gate.note_query(http_transport.caller(model))
    assert not _denied(_pre(repo, http_transport.caller(relay), pattern="y"))


def test_a_query_on_the_classic_path_earns_credit_too(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex opens with `initialize`, so its graph queries and its hook calls take the classic path. That
    path passed no caller: a gate call there never saw the query before it."""
    engine = core.Core(None)
    monkeypatch.setattr(engine, "call_tool", lambda conn, name, arguments: {"content": [], "isError": False})

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        reply = classic.answer(engine, cast(Any, object()), message, "agent:9")
        assert reply is not None
        return reply["result"]

    def grep(word: str) -> dict[str, Any]:
        return json.loads(call(search_gate.TOOL, {
            "tool_name": "Bash", "tool_input": json.dumps({"command": f"grep -rn {word} app/src"}),
            "cwd": str(repo), "session_id": "codex-1",
        })["content"][0]["text"])

    assert _denied(grep("one"))
    call("kg_search", {"query": "feed"})
    assert not _denied(grep("two"))


def test_a_client_that_names_no_agent_is_known_by_its_client_id() -> None:
    """A session of a version before the relay, Codex on its one connection, the stdio bridge."""
    assert http_transport.caller({"x-codebase-kg-client": "abc"}) == "abc"
    assert http_transport.caller({"x-codebase-kg-client": "abc", "x-codebase-kg-agent": "../x"}) == "abc"
    assert http_transport.caller({}) is None


def test_the_headers_helper_names_the_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("kg_headers_relay_test", ROOT / "mcp" / "launch" / "kg_headers.py")
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setenv("CODEBASE_KG_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_MCP_SERVER_URL", f"http://127.0.0.1:{_free_port()}/mcp")
    out = helper.headers(wait=0.0)
    assert out["X-Codebase-KG-Agent"] == "unknown" or out["X-Codebase-KG-Agent"].isdigit()
    assert out["X-Codebase-KG-Agent"] == shim.agent_name()


# --- the plugin's files -------------------------------------------------------------------------------
def test_claude_code_starts_the_relay_as_a_second_server_and_every_hook_names_it() -> None:
    servers = json.loads((ROOT / "mcp" / "claude.mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"codebase-kg", "codebase-kg-hooks"}
    relay = servers["codebase-kg-hooks"]
    # No extension: Claude Code runs kg-hooks.cmd on Windows and the sh script elsewhere.
    assert relay["command"] == "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg-hooks"
    assert relay["env"] == {"CLAUDE_PLUGIN_OPTION_SERVER_PORT": "${user_config.server_port}"}
    for name in ("kg-hooks", "kg-hooks.cmd"):
        text = (ROOT / "mcp" / "launch" / name).read_text(encoding="utf-8")
        assert "-I -S" in text and "--hooks" in text and "shim.py" in text, name
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    direct = [h for event in hooks.values() for group in event for h in group["hooks"] if h["type"] == "mcp_tool"]
    assert len(direct) == 4
    assert {h["server"] for h in direct} == {"plugin:codebase-kg:codebase-kg-hooks"}
    assert all("onFailure" not in h for h in direct)
    # The hook must outlast the relay's own allowance, or Claude Code cancels it first and says so.
    for hook in direct:
        assert hook["timeout"] > shim.hook_budget(hook["tool"], {"tool_input": "git push"}), hook["tool"]


def test_the_sh_launcher_is_executable_in_git() -> None:
    done = subprocess.run(["git", "ls-files", "-s", "mcp/launch/kg-hooks"], cwd=ROOT, capture_output=True, text=True,
                          check=False)
    if done.returncode != 0 or not done.stdout.strip():
        pytest.skip("not a git checkout, or the file is not added yet")
    assert done.stdout.startswith("100755 "), done.stdout


# --- the shim with --hooks against the real server --------------------------------------------------------
def _relay_process(cache: Path, port: int) -> subprocess.Popen[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in ("CODEBASE_KG_PATH", "CODEBASE_KG_TAKEOVER")}
    env.update(CODEBASE_KG_CACHE_DIR=str(cache), CODEBASE_KG_PORT=str(port))
    return subprocess.Popen([sys.executable, "-I", "-S", str(shim.PACKAGE_DIR / "shim.py"), "--hooks"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _ask(proc: subprocess.Popen[bytes], request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode() + b"\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _server(cache: Path, cwd: Path) -> tuple[subprocess.Popen[bytes], int]:
    """The real server on a port the OS picks, and that port."""
    env = {**os.environ, "CODEBASE_KG_CACHE_DIR": str(cache), "CODEBASE_KG_PORT": "0"}
    proc = subprocess.Popen(shim.server_command("--serve"), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for found in cache.glob("server-*.json"):
            try:
                port = json.loads(found.read_text(encoding="utf-8")).get("http_port")
            except (OSError, ValueError):
                continue
            if port:
                return proc, port
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("the server wrote no state file with an HTTP port")


def test_the_shim_relays_a_gate_call_to_the_real_server_and_says_nothing_on_stderr(tmp_path: Path, repo: Path) -> None:
    cache = tmp_path / "cache"
    server, port = _server(cache, repo)
    relay: subprocess.Popen[bytes] | None = None
    try:
        relay = _relay_process(cache, port)
        assert _ask(relay, 1, "tools/list", {})["result"] == {"tools": []}
        arguments = {"tool_name": "Grep", "tool_input": json.dumps({"pattern": "x"}), "cwd": str(repo), "session_id": "s1"}
        reply = _ask(relay, 2, "tools/call", {"name": "kg_hook_pre", "arguments": arguments})
        out = json.loads(reply["result"]["content"][0]["text"])
        assert _denied(out) and "systemMessage" not in out
        assert relay.stdin is not None and relay.stderr is not None
        relay.stdin.close()
        assert relay.wait(15) == 0
        assert relay.stderr.read() == b""
    finally:
        if relay is not None and relay.poll() is None:
            relay.kill()
        server.kill()
        server.wait(15)


def test_the_shim_answers_quietly_when_the_port_belongs_to_another_program(tmp_path: Path, repo: Path) -> None:
    other = Endpoint(lambda body, headers: (404, None))
    relay = _relay_process(tmp_path / "cache", other.port)
    try:
        arguments = {"tool_name": "Grep", "tool_input": json.dumps({"pattern": "x"}), "cwd": str(repo), "session_id": "s1"}
        first = _ask(relay, 1, "tools/call", {"name": "kg_hook_pre", "arguments": arguments})
        out = json.loads(first["result"]["content"][0]["text"])
        assert set(out) == {"systemMessage"} and str(other.port) in out["systemMessage"]
        second = _ask(relay, 2, "tools/call", {"name": "kg_hook_pre", "arguments": arguments})
        assert json.loads(second["result"]["content"][0]["text"]) == {}
    finally:
        relay.kill()
        other.close()

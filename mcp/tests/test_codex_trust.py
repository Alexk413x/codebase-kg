"""`codex-config` says whether Codex trusts the plugin's hooks, and says nothing when it cannot tell."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import codex_config, codex_trust


def _hook(status: str, plugin: str | None = "codebase-kg@alexk413x", enabled: bool = True) -> dict[str, Any]:
    return {"key": "k", "pluginId": plugin, "trustStatus": status, "enabled": enabled}


def _result(*hooks: dict[str, Any]) -> dict[str, Any]:
    return {"data": [{"cwd": "x", "hooks": list(hooks), "warnings": [], "errors": []}]}


def test_only_this_plugins_enabled_hooks_count() -> None:
    result = _result(_hook("trusted"), _hook("modified"), _hook("untrusted", plugin="other@market"),
                     _hook("untrusted", plugin=None), _hook("untrusted", enabled=False))
    assert codex_trust.statuses(result) == ["trusted", "modified"]


@pytest.mark.parametrize("result", [None, {}, {"data": None}, {"data": [{}]}, {"data": [{"hooks": [3]}]},
                                    {"data": [{"hooks": [{"pluginId": "codebase-kg@x", "trustStatus": 3}]}]}, "text"])
def test_an_answer_of_another_shape_gives_nothing(result: Any) -> None:
    assert codex_trust.statuses(result) is None


def test_the_message_names_the_count_what_is_lost_and_the_command() -> None:
    text = codex_trust.message(["untrusted", "modified", "trusted"])
    assert text is not None and "2 of 3" in text
    assert "/hooks" in text and "pre-push" in text and "every plugin update" in text


def test_trusted_and_managed_hooks_need_no_review() -> None:
    assert codex_trust.message(["trusted", "managed"]) == "Codex trusts all 2 codebase-kg hooks."


def test_no_hooks_of_this_plugin_says_nothing() -> None:
    assert codex_trust.message([]) is None


def _server(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


ANSWERING = '''
    import json, sys
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            print(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
        elif message.get("method") == "hooks/list":
            print("not json", flush=True)
            print(json.dumps({"method": "some/notification", "params": {}}), flush=True)
            hooks = [{"pluginId": "codebase-kg@alexk413x", "trustStatus": "modified", "enabled": True}]
            print(json.dumps({"id": message["id"], "result": {"data": [{"cwd": message["params"]["cwds"][0],
                                                                        "hooks": hooks}]}}), flush=True)
'''


def test_the_exchange_reads_the_hooks_list_answer(tmp_path: Path) -> None:
    result = codex_trust._exchange(_server(tmp_path, ANSWERING), str(tmp_path), 20)
    assert codex_trust.statuses(result) == ["modified"]


@pytest.mark.parametrize("body", [
    "import sys\nsys.exit(3)\n",
    "import sys\nfor line in sys.stdin:\n    print('garbage', flush=True)\n    break\n",
    "import json, sys\nfor line in sys.stdin:\n    print(json.dumps({'id': 0, 'error': {'code': 1}}), flush=True)\n",
    "import time\ntime.sleep(30)\n",
])
def test_a_server_that_fails_ends_early_or_never_answers_gives_nothing(tmp_path: Path, body: str) -> None:
    assert codex_trust._exchange(_server(tmp_path, body), str(tmp_path), 1.5) is None


def test_a_command_that_does_not_exist_gives_nothing(tmp_path: Path) -> None:
    assert codex_trust._exchange([str(tmp_path / "no-such-codex")], str(tmp_path), 1) is None


def test_no_codex_on_path_gives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert codex_trust.report() is None


def test_a_raising_exchange_never_reaches_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("shutil.which", lambda name: "codex")
    monkeypatch.setattr(codex_trust, "_exchange", boom)
    assert codex_trust.report() is None


def test_codex_config_prints_the_trust_line_as_toml_comments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(codex_trust, "report", lambda: codex_trust.message(["untrusted"] * 5))
    assert codex_config.main([]) == 0
    out = capsys.readouterr().out
    tail = out.split("tool_timeout_sec", 1)[1].splitlines()[1:]
    assert tail[0] == "" and all(line.startswith("# ") for line in tail[1:])
    assert "5 of 5" in " ".join(tail) and "/hooks" in " ".join(tail)


def test_codex_config_prints_nothing_extra_when_the_check_gives_nothing_or_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(codex_trust, "report", lambda: None)
    assert codex_config.main([]) == 0
    assert capsys.readouterr().out.rstrip().endswith(f"tool_timeout_sec = {codex_config.TOOL_TIMEOUT}")

    def never() -> str:
        raise AssertionError("--no-hooks must not ask Codex")

    monkeypatch.setattr(codex_trust, "report", never)
    assert codex_config.main(["--no-hooks"]) == 0

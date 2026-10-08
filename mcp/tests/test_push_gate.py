"""`hooks/kg_push_gate.py` — the PreToolUse gate on an agent's `git push`.

The pre-push git hook blocks a push over stale mapped files, but an agent can
retry past it with `--no-verify`. This hook denies earlier, in the agent's own
turn, on the same rule. These tests pin command detection, the allow paths, the
deny message, and that any error lets the call through.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import kg_push_gate as gate  # noqa: E402

KNOWN = "ui/Known.kt"
RANKER = "domain/Ranker.kt"


# --- command detection -------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git push --force-with-lease --no-verify",
        "git -C /srv/app push",
        "git -c core.editor=true push",
        "git --no-pager push",
        "git status && git push",
        "git add . ; git push origin HEAD",
        "git fetch || git push",
        "make test | tee log && git push",
        "cd /srv/app && git push",
        "(cd /srv/app && git push)",
        "SKIP_KG=1 git push",
        "env FOO=1 git push",
        "command git push",
        "/usr/bin/git push",
        "git.exe push",
        "bash -c 'git push origin main'",
        "sh -c \"git add . && git push\"",
        "git push 2>&1 | tail",
        "git push\ngit status",
        "git status\ngit push",
    ],
)
def test_a_git_push_is_detected(command: str) -> None:
    assert len(gate.find_pushes(command, Path("/repo"))) == 1


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git pull",
        "git commit -m 'push the button'",
        "git commit -m \"git push\"",
        "git stash push",
        "git log --grep=push",
        "git remote set-url origin x && git fetch",
        "echo git push",
        "echo 'git push'",
        "grep -rn push src",
        "ls",
        "bash -c 'git status'",
        "pushd /tmp",
        "",
    ],
)
def test_other_commands_are_not_a_push(command: str) -> None:
    assert gate.find_pushes(command, Path("/repo")) == []


def test_each_push_in_a_chain_is_found() -> None:
    pushes = gate.find_pushes("git push && git push origin v1", Path("/repo"))
    assert len(pushes) == 2


def test_dash_c_dir_moves_the_push_directory(tmp_path: Path) -> None:
    (push,) = gate.find_pushes("git -C sub/app push", tmp_path)
    assert push.cwd == tmp_path / "sub" / "app"


def test_an_absolute_dash_c_dir_replaces_the_base(tmp_path: Path) -> None:
    other = tmp_path / "other"
    (push,) = gate.find_pushes(f"git -C {other.as_posix()} push", tmp_path / "base")
    assert push.cwd == other


def test_cd_moves_later_pushes_only(tmp_path: Path) -> None:
    (push,) = gate.find_pushes("git status && cd pkg && git push", tmp_path)
    assert push.cwd == tmp_path / "pkg"


def test_the_env_prefix_travels_with_the_push(tmp_path: Path) -> None:
    (push,) = gate.find_pushes("SKIP_KG=1 KG_STALE_ACK=3 git push", tmp_path)
    assert push.env == {"SKIP_KG": "1", "KG_STALE_ACK": "3"}
    (push,) = gate.find_pushes("env KG_STALE_ACK=4 git push", tmp_path)
    assert push.env == {"KG_STALE_ACK": "4"}


def test_an_export_applies_to_later_pushes_only(tmp_path: Path) -> None:
    before, after = (
        gate.find_pushes("git push; export SKIP_KG=1; git push", tmp_path)[i] for i in (0, 1)
    )
    assert before.env == {} and after.env == {"SKIP_KG": "1"}


def test_unbalanced_quotes_raise_so_the_caller_can_fail_open() -> None:
    with pytest.raises(ValueError):
        gate.find_pushes("git push 'oops", Path("/repo"))


# --- a real repo -------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    cmd = [
        "git", "-c", "user.name=t", "-c", "user.email=t@example.com",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
    ]
    return subprocess.run(
        cmd, cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _make_repo(tmp_path: Path, drift: tuple[str, ...] = ()) -> Path:
    """A committed repo whose graph baselines match the source, then `drift`
    files are rewritten and committed so HEAD no longer matches the graph."""
    repo = tmp_path / "repo"
    src = repo / "src"
    (src / "ui").mkdir(parents=True)
    (src / "domain").mkdir(parents=True)
    (src / KNOWN).write_text("class Known { fun a() {} }", encoding="utf-8")
    (src / RANKER).write_text("class Ranker { fun b() {} }", encoding="utf-8")
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [
            Node(id="known", kind="K", anchors=[Anchor(KNOWN, "Known")]),
            Node(id="ranker", kind="K", anchors=[Anchor(RANKER, "Ranker")]),
        ],
        source_root=src,
    )
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for rel in drift:
        (src / rel).write_text("class Rewritten { fun changed() {} }", encoding="utf-8")
    if drift:
        _git(repo, "commit", "-q", "-am", "drift")
    return repo


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKIP_KG", raising=False)
    monkeypatch.delenv("KG_STALE_ACK", raising=False)
    monkeypatch.chdir(tmp_path)  # the gate changes directory into the repo


def _call(command: str, cwd: Path, capsys: pytest.CaptureFixture) -> str:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    gate._run(payload)
    return capsys.readouterr().out


def test_a_stale_repo_denies_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    out = json.loads(_call("git push origin main", repo, capsys))
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert f"src/{KNOWN}" in reason
    assert "1 mapped file(s)" in reason
    assert "/codebase-kg:refresh" in reason
    assert "knowledge/code_graph.db" in reason
    assert "git push again" in reason
    assert "KG_STALE_ACK=1" in reason


def test_the_deny_lists_every_stale_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN, RANKER))
    reason = json.loads(_call("git push", repo, capsys))["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert f"src/{KNOWN}" in reason and f"src/{RANKER}" in reason
    assert "2 mapped file(s)" in reason and "KG_STALE_ACK=2" in reason


def test_a_clean_repo_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path)
    assert _call("git push", repo, capsys) == ""


def test_dash_c_checks_the_named_repo_not_the_cwd(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _call(f"git -C {repo.as_posix()} push", elsewhere, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_chained_push_is_denied(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    out = _call("git add -A && git commit -m x && git push", repo, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_verify_does_not_open_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    out = _call("git push --no-verify", repo, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_skip_kg_in_the_command_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    assert _call("SKIP_KG=1 git push", repo, capsys) == ""
    assert _call("export SKIP_KG=1 && git push", repo, capsys) == ""


def test_skip_kg_in_the_process_env_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    monkeypatch.setenv("SKIP_KG", "1")
    assert _call("git push", repo, capsys) == ""


def test_a_matching_ack_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    assert _call("KG_STALE_ACK=1 git push", repo, capsys) == ""
    monkeypatch.setenv("KG_STALE_ACK", "1")
    assert _call("git push", repo, capsys) == ""


def test_a_wrong_ack_does_not_allow_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    out = _call("KG_STALE_ACK=7 git push", repo, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setenv("KG_STALE_ACK", "7")
    out = _call("git push", repo, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_ack_in_one_command_does_not_cover_another_push(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    out = _call("KG_STALE_ACK=1 git push; git push", repo, capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_repo_without_a_graph_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    assert _call("git push", repo, capsys) == ""


def test_a_directory_that_is_not_a_repo_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert _call("git push", tmp_path, capsys) == ""


@pytest.mark.parametrize("command", ["git status", "git commit -m 'git push'", "ls"])
def test_other_commands_never_reach_the_check(
    command: str, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_repo: Path) -> None:
        raise AssertionError("the check ran")

    monkeypatch.setattr(gate, "stale_files", boom)
    assert _call(command, Path("."), capsys) == ""


def test_other_tools_are_ignored(capsys: pytest.CaptureFixture) -> None:
    gate._run({"tool_name": "Edit", "tool_input": {"command": "git push"}})
    assert capsys.readouterr().out == ""


# --- the message -------------------------------------------------------------
def test_the_list_is_capped_with_a_count() -> None:
    stale = [f"src/f{i}.kt" for i in range(gate.LIST_CAP + 5)]
    reason = gate.deny_reason(stale, "knowledge/code_graph.db")
    assert f"{len(stale)} mapped file(s)" in reason
    assert "src/f0.kt" in reason and f"src/f{gate.LIST_CAP - 1}.kt" in reason
    assert f"src/f{gate.LIST_CAP}.kt" not in reason
    assert "... and 5 more" in reason
    assert f"KG_STALE_ACK={len(stale)}" in reason


def test_a_short_list_has_no_more_line() -> None:
    assert "more" not in gate.deny_reason(["src/a.kt"], "knowledge/code_graph.db")


def test_the_message_names_the_configured_graph_path() -> None:
    assert "docs/graph.db" in gate.deny_reason(["a.kt"], "docs/graph.db")


# --- fail open ---------------------------------------------------------------
def _run_main(monkeypatch: pytest.MonkeyPatch, stdin_text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    gate.main()


def test_an_error_in_the_check_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))

    def boom(_repo: Path) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gate, "stale_files", boom)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(repo)}
    _run_main(monkeypatch, json.dumps(payload))
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "stdin_text",
    [
        "",
        "not json",
        "[]",
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": "git push"}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push 'oops"}}),
    ],
)
def test_a_malformed_payload_allows_the_call(
    stdin_text: str, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_main(monkeypatch, stdin_text)
    assert capsys.readouterr().out == ""


def test_an_unreadable_graph_allows_the_push(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, drift=(KNOWN,))
    (repo / "knowledge" / "code_graph.db").write_text("junk", encoding="utf-8")
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(repo)}
    _run_main(monkeypatch, json.dumps(payload))
    assert capsys.readouterr().out == ""

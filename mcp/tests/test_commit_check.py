"""The commit-time graph check the server answers for a `PostToolUse` hook.

The hook runs after a shell command that ran git. The server reports the commits
that command made, once, and reports nothing for a command that made none.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codebase_kg import classic, commit_check, core
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parents[2]
GRAPH = "knowledge/code_graph.db"
KNOWN = "src/ui/Known.kt"
RANKER = "src/domain/Ranker.kt"
COMMIT = "git commit -m change"


@pytest.fixture(autouse=True)
def _forget_reported_commits() -> Iterator[None]:
    commit_check._reported.clear()
    yield
    commit_check._reported.clear()


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    return done.stdout.strip()


def _build(repo: Path) -> None:
    build(
        repo / GRAPH,
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [
            Node(id="known", kind="K", anchors=[Anchor("ui/Known.kt", "Known")]),
            Node(id="ranker", kind="K", anchors=[Anchor("domain/Ranker.kt", "Ranker")]),
        ],
        source_root=repo / "src",
    )


def _refresh(repo: Path) -> None:
    graph = CodeGraph(repo / GRAPH)
    try:
        meta, nodes = graph.meta, graph.all_nodes()
    finally:
        graph.close()
    build(repo / GRAPH, meta, nodes, source_root=repo / "src")


def _tree(repo: Path) -> Path:
    for rel, text in ((KNOWN, "class Known { fun a() {} }"), (RANKER, "class Ranker { fun b() {} }")):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _build(repo)
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _commit_all(repo: Path, message: str = "change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository whose first commit holds two mapped files and their graph, already reported."""
    made = _tree(tmp_path / "repo")
    _commit_all(made, "first")
    assert commit_check.check(COMMIT, str(made)) is None
    return made


def _edit(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m x", ["."]),
        ("git add -A && git commit -m 'a; b'", ["."]),
        ("git commit -am x", ["."]),
        ("GIT_AUTHOR_NAME=a git -c user.name=b commit -m x", ["."]),
        ("git -C sub commit -m x", ["sub"]),
        ("cd sub && git commit -m x", ["sub"]),
        ("Set-Location sub; git commit -m x", ["sub"]),
        ("git -C one commit -m x; git -C two commit -m y", ["one", "two"]),
        ("bash -c 'cd sub && git commit -m x'", ["sub"]),
        ("git.exe commit -m x", ["."]),
        ("git commit --dry-run", []),
        ("git log --grep commit", []),
        ("git status", []),
        ("echo git commit", []),
    ],
)
def test_the_folders_a_command_commits_in(tmp_path: Path, command: str, expected: list[str]) -> None:
    assert commit_check.commit_dirs(command, tmp_path) == [tmp_path / part for part in expected]


def test_a_command_it_cannot_read_falls_back_to_the_session_folder(tmp_path: Path) -> None:
    assert commit_check.commit_dirs("git commit -m 'unclosed", tmp_path) == [tmp_path]
    assert commit_check.commit_dirs("echo 'unclosed", tmp_path) == []


def test_a_windows_path_keeps_its_separators(tmp_path: Path) -> None:
    assert commit_check.commit_dirs(r"git -C C:\work\repo commit -m x", tmp_path) == [tmp_path / r"C:\work\repo"]


def test_a_commit_of_unmapped_changed_and_deleted_source_lists_each(repo: Path) -> None:
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _edit(repo, "src/ui/New.kt", "class New")
    (repo / RANKER).unlink()
    head = _commit_all(repo)

    said = commit_check.check("git add -A && git commit -m change", str(repo))

    assert said is not None
    assert f"commit {head[:9]}" in said
    assert f"  ~ {KNOWN}" in said and "  + src/ui/New.kt" in said and f"  - {RANKER}" in said
    assert "/codebase-kg:refresh" in said and "follow-up commit" in said and "without asking the user" in said
    assert "amend" not in said


def test_a_commit_is_reported_once(repo: Path) -> None:
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _commit_all(repo)
    assert commit_check.check(COMMIT, str(repo)) is not None
    assert commit_check.check(COMMIT, str(repo)) is None


def test_a_commit_of_mapped_unchanged_source_says_nothing(repo: Path) -> None:
    _edit(repo, "notes.md", "not source")
    _commit_all(repo)
    assert commit_check.check(COMMIT, str(repo)) is None


def test_a_failed_commit_reports_nothing(repo: Path) -> None:
    """Nothing is staged, so git makes no commit and HEAD is the commit already reported."""
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "-m", "nothing staged")
    assert _git(repo, "rev-parse", "HEAD") == before
    assert commit_check.check(COMMIT, str(repo)) is None


def test_an_old_head_reports_nothing(tmp_path: Path) -> None:
    """A server that never saw this repository does not report a commit made before the command."""
    made = _tree(tmp_path / "repo")
    _edit(made, "src/ui/New.kt", "class New")
    _commit_all(made, "first")
    later = time.time() + commit_check.RECENT + 60
    assert commit_check.check(COMMIT, str(made), now=later) is None


def test_several_commits_from_one_command_are_one_report(repo: Path) -> None:
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _commit_all(repo, "one")
    _edit(repo, "src/ui/New.kt", "class New")
    _commit_all(repo, "two")

    said = commit_check.check("git commit -m one && git commit -m two", str(repo))

    assert said is not None and "the last 2 commits" in said
    assert f"  ~ {KNOWN}" in said and "  + src/ui/New.kt" in said


def test_the_first_commit_of_a_repository_is_checked(tmp_path: Path) -> None:
    made = _tree(tmp_path / "repo")
    _edit(made, "src/ui/New.kt", "class New")
    _commit_all(made, "first")

    said = commit_check.check(COMMIT, str(made))

    assert said is not None and "  + src/ui/New.kt" in said
    assert KNOWN not in said


def test_a_merge_commit_is_compared_with_the_branch_it_landed_on(repo: Path) -> None:
    _git(repo, "switch", "-q", "-c", "side")
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _commit_all(repo, "side change")
    assert commit_check.check(COMMIT, str(repo)) is not None
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    _git(repo, "commit", "-q", "-m", "merge side")
    assert "commit (merge)" in _git(repo, "reflog", "-1")

    said = commit_check.check("git merge --no-commit side && git commit -m merge", str(repo))

    assert said is not None and f"  ~ {KNOWN}" in said and "commit " in said


def _side_branch_with_unmapped_file(repo: Path) -> None:
    """`side` holds one commit that adds unmapped source; `main` has moved on by a notes file."""
    _git(repo, "switch", "-q", "-c", "side")
    _edit(repo, "src/ui/Theirs.kt", "class Theirs")
    _commit_all(repo, "their work")
    _git(repo, "switch", "-q", "main")
    _edit(repo, "notes.md", "main moved")
    _commit_all(repo, "notes")
    commit_check._reported.clear()


def test_a_commit_right_after_a_rebase_reports_only_that_commit(repo: Path) -> None:
    """A rebase rewrites committer times, so the rebased commits look new. The reflog says who made them."""
    _side_branch_with_unmapped_file(repo)
    _git(repo, "switch", "-q", "side")
    _git(repo, "rebase", "-q", "main")
    assert "rebase" in _git(repo, "reflog", "-1")
    assert commit_check.check("git rebase main && git commit -m x", str(repo)) is None

    _edit(repo, "src/ui/Mine.kt", "class Mine")
    head = _commit_all(repo, "my work")
    said = commit_check.check("git rebase main && git add -A && git commit -m mine", str(repo))

    assert said is not None and f"commit {head[:9]}" in said
    assert "  + src/ui/Mine.kt" in said and "Theirs.kt" not in said


def test_a_commit_right_after_a_pull_reports_only_that_commit(repo: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _edit(repo, "src/ui/Theirs.kt", "class Theirs")
    _commit_all(repo, "their work")
    _git(clone, "pull", "-q", "--ff-only")
    assert (clone / "src/ui/Theirs.kt").is_file()
    commit_check._reported.clear()
    assert commit_check.check("git pull && git commit -m x", str(clone)) is None

    _edit(clone, "src/ui/Mine.kt", "class Mine")
    _commit_all(clone, "my work")
    said = commit_check.check("git pull && git add -A && git commit -m mine", str(clone))

    assert said is not None and "  + src/ui/Mine.kt" in said and "Theirs.kt" not in said


def test_an_amend_of_a_reported_commit_reports_what_the_amend_added(repo: Path) -> None:
    _edit(repo, "src/ui/First.kt", "class First")
    _commit_all(repo, "work")
    first = commit_check.check(COMMIT, str(repo))
    assert first is not None and "  + src/ui/First.kt" in first

    _edit(repo, "src/ui/Second.kt", "class Second")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    said = commit_check.check("git add -A && git commit --amend --no-edit", str(repo))

    assert said is not None and "  + src/ui/Second.kt" in said and "First.kt" not in said


def test_a_commit_and_its_amend_from_one_command_report_the_whole_change(repo: Path) -> None:
    _edit(repo, "src/ui/First.kt", "class First")
    _commit_all(repo, "work")
    _edit(repo, "src/ui/Second.kt", "class Second")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "--no-edit")

    said = commit_check.check("git commit -m work && git commit --amend --no-edit", str(repo))

    assert said is not None and "  + src/ui/First.kt" in said and "  + src/ui/Second.kt" in said


def test_a_repository_without_a_reflog_checks_head_only(repo: Path) -> None:
    _git(repo, "config", "core.logAllRefUpdates", "false")
    shutil.rmtree(repo / ".git" / "logs")
    _edit(repo, "src/ui/One.kt", "class One")
    _commit_all(repo, "one")
    _edit(repo, "src/ui/Two.kt", "class Two")
    _commit_all(repo, "two")

    said = commit_check.check("git commit -m one && git commit -m two", str(repo))

    assert said is not None and "  + src/ui/Two.kt" in said and "One.kt" not in said
    assert commit_check.check(COMMIT, str(repo)) is None


def test_a_check_that_fails_is_tried_again(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _edit(repo, "src/ui/New.kt", "class New")
    _commit_all(repo)
    checker = commit_check._load_checker()
    real = checker.read_graph

    def locked(db: Path) -> Any:
        raise OSError("graph is locked")

    monkeypatch.setattr(checker, "read_graph", locked)
    assert _hook_json(commit_check.call({"command": COMMIT, "cwd": str(repo)})) == {}
    monkeypatch.setattr(checker, "read_graph", real)

    said = commit_check.check(COMMIT, str(repo))
    assert said is not None and "  + src/ui/New.kt" in said


def test_a_per_clone_file_with_retired_keys_still_loads(repo: Path) -> None:
    _edit(repo, ".claude/codebase-kg.local.md", "---\npost_edit_nudge: true\nnudge_every: 5\n---\n")
    _edit(repo, "src/ui/New.kt", "class New")
    _commit_all(repo)

    said = commit_check.check(COMMIT, str(repo))

    assert said is not None and "  + src/ui/New.kt" in said
    assert "post_edit_nudge" not in said and "nudge_every" not in said


def test_a_commit_in_another_folder_is_found_from_the_command(repo: Path, tmp_path: Path) -> None:
    _edit(repo, "src/ui/New.kt", "class New")
    _commit_all(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert commit_check.check(COMMIT, str(elsewhere)) is None
    said = commit_check.check(f'git -C "{repo}" commit -m change', str(elsewhere))
    assert said is not None and "  + src/ui/New.kt" in said


def test_a_refresh_leaves_nothing_to_report(repo: Path) -> None:
    """The agent refreshes and commits the graph. That commit, and the next edit-free one, say nothing."""
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _commit_all(repo)
    assert commit_check.check(COMMIT, str(repo)) is not None

    _refresh(repo)
    _commit_all(repo, "refresh the graph")
    assert commit_check.check(COMMIT, str(repo)) is None

    commit_check._reported.clear()
    assert commit_check.check(COMMIT, str(repo)) is None


def test_source_and_its_refreshed_graph_in_one_commit_say_nothing(repo: Path) -> None:
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _refresh(repo)
    _commit_all(repo)
    assert commit_check.check(COMMIT, str(repo)) is None


def test_the_check_can_be_turned_off_per_clone(repo: Path) -> None:
    _edit(repo, ".claude/codebase-kg.local.md", "---\ncommit_check: false\n---\n")
    _edit(repo, KNOWN, "class Known { fun gutted() {} }")
    _commit_all(repo)
    assert commit_check.check(COMMIT, str(repo)) is None


def test_a_repository_without_a_graph_says_nothing(tmp_path: Path) -> None:
    bare = tmp_path / "plain"
    _edit(bare, "src/a.py", "x = 1")
    _git(bare, "init", "-q", "-b", "main")
    _commit_all(bare, "first")
    assert commit_check.check(COMMIT, str(bare)) is None


def test_a_folder_that_is_no_repository_says_nothing(tmp_path: Path) -> None:
    assert commit_check.check(COMMIT, str(tmp_path)) is None
    assert commit_check.check(COMMIT, "relative/folder") is None


def _hook_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False and "structuredContent" not in result
    return json.loads(result["content"][0]["text"])


def test_the_tool_answers_with_post_tool_use_context(repo: Path) -> None:
    _edit(repo, "src/ui/New.kt", "class New")
    _commit_all(repo)

    out = _hook_json(commit_check.call({"command": COMMIT, "cwd": str(repo)}))

    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "  + src/ui/New.kt" in out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("args", [None, {}, {"command": 3, "cwd": 4}, {"command": "git status", "cwd": "C:/x"}, "text"])
def test_the_tool_answers_an_empty_object_when_it_has_nothing_to_say(args: Any) -> None:
    assert _hook_json(commit_check.call(args)) == {}


def test_a_failing_check_answers_an_empty_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(command: str, cwd: str, now: float | None = None) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(commit_check, "check", boom)
    assert _hook_json(commit_check.call({"command": COMMIT, "cwd": str(tmp_path)})) == {}


def test_the_hook_endpoint_is_not_in_the_catalog_and_needs_no_session() -> None:
    assert commit_check.TOOL not in core.TOOLS
    assert core.hook_result("kg_search", {}) is None
    reply = classic.answer(core.Core(None), None, {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": commit_check.TOOL, "arguments": {"command": "git status", "cwd": "C:/x"}},
    })
    assert reply is not None and _hook_json(reply["result"]) == {}


def test_claude_code_calls_the_endpoint_after_git_commands() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    entries = {group["matcher"]: group["hooks"] for group in hooks["PostToolUse"]}
    assert set(entries) == {"Bash", "PowerShell"}
    for shell, (handler,) in entries.items():
        assert handler["type"] == "mcp_tool"
        assert handler["server"] == "plugin:codebase-kg:codebase-kg-hooks"
        assert handler["tool"] == commit_check.TOOL
        assert handler["if"] == f"{shell}(git *)"
        assert handler["input"] == {"command": "${tool_input.command}", "cwd": "${cwd}"}
        assert "onFailure" not in handler


def test_loading_the_checker_writes_no_bytecode_into_the_plugin(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = commit_check.CHECKER.parent / "__pycache__"
    before = sorted(p.name for p in cache.iterdir()) if cache.is_dir() else None
    monkeypatch.setattr(commit_check, "_checker", None)

    commit_check.check(COMMIT, str(repo))

    assert (sorted(p.name for p in cache.iterdir()) if cache.is_dir() else None) == before
    assert commit_check._load_checker().GIT_CWD is None

"""Regression tests for the hook, git-hook and launcher defects from the review.

Each test pins one reproduced defect. The numbers in the comments are the
review's item numbers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

ROOT = Path(__file__).resolve().parent.parent.parent
HOOKS = ROOT / "hooks"
GIT_HOOKS = ROOT / "git-hooks"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(GIT_HOOKS))

import _config as cfg_mod
import kg_post_edit_check as post_edit
import kg_pre_commit as pre_commit
import kg_pre_push as pre_push
import kg_push_gate as push_gate
import kg_search_gate as search_gate
import kg_session_start as session_start

ZEROS = "0" * 40
KNOWN = "ui/Known.kt"
win32_only = pytest.mark.skipif(sys.platform != "win32", reason="Git Bash drive paths")


def _git(repo: Path, *args: str) -> str:
    cmd = [
        "git", "-c", "user.name=t", "-c", "user.email=t@example.com",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
    ]
    return subprocess.run(
        cmd, cwd=repo, check=True, capture_output=True, encoding="utf-8"
    ).stdout


def _gitbash(path: Path) -> str:
    """`C:\\a\\b` as Git Bash writes it: `/c/a/b`."""
    return "/" + path.drive[0].lower() + path.as_posix()[2:]


def _graph_repo(tmp_path: Path, drift: bool = False) -> Path:
    repo = tmp_path / "repo"
    src = repo / "src"
    (src / "ui").mkdir(parents=True)
    (src / KNOWN).write_text("class Known { fun a() {} }", encoding="utf-8")
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [Node(id="known", kind="K", anchors=[Anchor(KNOWN, "Known")])],
        source_root=src,
    )
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    if drift:
        (src / KNOWN).write_text("class Rewritten { fun changed() {} }", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "drift")
    return repo


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "hookstate"
    state.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(state))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)
    monkeypatch.delenv("KG_STALE_ACK", raising=False)


# --- 18: the nudge counter resets when the graph is rewritten ------------------
class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text


def test_the_nudge_counter_resets_when_the_graph_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _graph_repo(tmp_path)
    mapped = repo / "src" / KNOWN
    payload = json.dumps({
        "tool_name": "Edit", "tool_input": {"file_path": str(mapped)}, "cwd": str(repo),
    })

    def edit() -> list[dict[str, Any]]:
        monkeypatch.setattr(post_edit.sys, "stdin", _Stdin(payload))
        post_edit.main()
        out = capsys.readouterr().out.strip()
        return [json.loads(line) for line in out.splitlines()]

    for _ in range(4):
        assert edit() == []
    graph = repo / "knowledge" / "code_graph.db"
    later = graph.stat().st_mtime_ns + 5_000_000_000
    os.utime(graph, ns=(later, later))
    assert edit() == []


# --- 19: Git Bash drive paths --------------------------------------------------
@win32_only
def test_a_git_bash_drive_path_is_a_local_path() -> None:
    assert cfg_mod.as_local_path("/c/Users/me/repo") == Path("C:/Users/me/repo")
    assert cfg_mod.as_local_path("C:/Users/me/repo") == Path("C:/Users/me/repo")


@win32_only
def test_the_push_gate_follows_a_git_bash_cd(tmp_path: Path) -> None:
    (push,) = push_gate.find_pushes(
        f"cd {_gitbash(tmp_path)}/repo && git push", Path("D:/elsewhere")
    )
    assert push.cwd == tmp_path / "repo"
    (push,) = push_gate.find_pushes(
        f"git -C {_gitbash(tmp_path)}/repo push", Path("D:/elsewhere")
    )
    assert push.cwd == tmp_path / "repo"


@win32_only
def test_the_search_gate_follows_a_git_bash_cd(scoped: Path) -> None:
    assert search_gate.shell_search_is_gated(
        f"cd {_gitbash(scoped)}/src && grep -rn thing .", scoped
    ) is True
    assert search_gate.shell_search_is_gated(f"grep -rn thing {_gitbash(scoped)}/src", scoped) is True


# --- 20: heredocs in the push gate ---------------------------------------------
def test_an_apostrophe_in_a_heredoc_does_not_hide_a_push() -> None:
    command = "git commit -F - <<'EOF'\nit's a fix\nEOF\ngit push origin main"
    assert len(push_gate.find_pushes(command, Path("/repo"))) == 1


def test_a_push_line_inside_a_heredoc_is_data() -> None:
    command = "cat <<EOF\ngit push origin main\nEOF"
    assert push_gate.find_pushes(command, Path("/repo")) == []


# --- 21: the legacy kg_path key -------------------------------------------------
def test_a_local_md_with_only_kg_path_locates_the_graph(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\nkg_path: custom/g.db\n---\n", encoding="utf-8"
    )
    (tmp_path / "custom").mkdir()
    build(tmp_path / "custom" / "g.db", Meta(codebase="x"), [Node(id="a", kind="K")])
    cfg = cfg_mod.load_config(tmp_path)
    assert cfg_mod.find_graph(tmp_path, cfg) == (tmp_path / "custom" / "g.db").resolve()


def test_graph_path_outranks_kg_path(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\ngraph_path: new.db\nkg_path: old.db\n---\n", encoding="utf-8"
    )
    assert cfg_mod.load_config(tmp_path)["graph_path"] == "new.db"


# --- 22: a quoted pattern that starts with a redirect character -----------------
@pytest.fixture
def scoped(tmp_path: Path) -> Path:
    proj = tmp_path.resolve() / "proj"
    for sub in ("src", "docs", "node_modules"):
        (proj / sub).mkdir(parents=True)
    return proj


@pytest.mark.parametrize("pattern", ['"<Button"', "'>=2'", '"<<EOF"'])
def test_a_quoted_pattern_that_looks_like_a_redirect_is_still_a_search(
    pattern: str, scoped: Path
) -> None:
    assert search_gate.shell_search_is_gated(f"grep -rn {pattern} src/", scoped) is True
    parsed = search_gate.parse_shell_search(f"grep -rn {pattern} src/", scoped)
    assert parsed is not None and parsed.terms == [pattern[1:-1]]


def test_a_real_redirect_is_still_not_an_operand(scoped: Path) -> None:
    known = scoped / "src" / "known.py"
    known.write_text("x", encoding="utf-8")
    assert search_gate.shell_search_is_gated(f"grep -n thing {known} >out.txt", scoped) is False
    assert search_gate.shell_search_is_gated("grep -rn thing src/ <in.txt", scoped) is True


# --- 24: root and ignored directories on the shell path -------------------------
def test_a_shell_search_outside_the_root_is_not_gated(scoped: Path) -> None:
    assert search_gate.shell_search_is_gated("grep -rn thing docs", scoped, None, "src") is False
    assert search_gate.shell_search_is_gated("grep -rn thing src", scoped, None, "src") is True
    assert search_gate.shell_search_is_gated("grep -rn thing .", scoped, None, "src") is True


def test_a_shell_search_of_an_ignored_directory_is_not_gated(scoped: Path) -> None:
    assert search_gate.shell_search_is_gated("grep -rn thing node_modules", scoped) is False
    assert search_gate.shell_search_is_gated("find node_modules -name '*.js'", scoped) is False


def test_the_gate_passes_the_graph_root_to_the_shell_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _graph_repo(tmp_path)
    (repo / "docs").mkdir()

    def run(command: str) -> str:
        data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
        monkeypatch.setattr(search_gate.sys, "stdin", _Stdin(json.dumps(data)))
        search_gate.main()
        return capsys.readouterr().out

    assert run("grep -rn thing docs") == ""
    assert "deny" in run("grep -rn thing src")


# --- 25: a URL in the textconv is not a plugin path -----------------------------
@pytest.mark.parametrize("textconv", [
    "python3 https://raw.githubusercontent.com/Alexk413x/codebase-kg/main/kg_cli.py",
    "python3 ~/.claude/plugins/cache/codebase-kg/mcp/launch/kg_cli.py textconv",
    "python3 ${CLAUDE_PLUGIN_ROOT}/codebase-kg/mcp/launch/kg_cli.py",
])
def test_a_textconv_the_hook_cannot_check_is_not_reported_missing(textconv: str) -> None:
    assert session_start.stale_pin(textconv, "0.14.4") is None


def test_a_missing_plugin_folder_is_still_reported(tmp_path: Path) -> None:
    gone = (tmp_path / "gone" / "codebase-kg" / "kg_cli.py").as_posix()
    assert session_start.stale_pin(f'python3 "{gone}" textconv', "0.14.4") is not None


# --- 26: config encoding, here-strings, cd order, push wrappers -----------------
def test_a_bom_in_the_local_md_does_not_disable_it(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "\ufeff---\nsearch_gate: off\nroot: app\n---\n", encoding="utf-8"
    )
    cfg = cfg_mod.load_config(tmp_path)
    assert cfg["search_gate"] is False and cfg["root"] == "app"
    assert pre_push.load_config(tmp_path) == {"search_gate": "off", "root": "app"}


def test_a_local_md_that_is_not_utf8_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_bytes(b"---\nroot: caf\xe9\n---\n")
    assert cfg_mod.load_config(tmp_path) == cfg_mod.DEFAULTS
    assert pre_push.load_config(tmp_path) == {}


def test_a_here_string_is_not_a_heredoc() -> None:
    command = 'cat <<< "word"\ngrep -rn thing src\n'
    assert search_gate.strip_heredocs(command) == command.rstrip("\n")


def test_a_cd_moves_later_clauses_only(scoped: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert search_gate.shell_search_is_gated(f"grep -rn thing src && cd {other}", scoped) is True
    assert search_gate.shell_search_is_gated(f"cd {other} && grep -rn thing .", scoped) is False


@pytest.mark.parametrize("command", [
    "if git push; then echo ok; fi",
    "! git push",
    "sudo -u deploy git push",
    "sudo -E -u deploy git push origin main",
    "env -u HTTP_PROXY git push",
    "bash -lc 'git push'",
    "sh -xc \"cd /srv/app && git push\"",
])
def test_a_push_behind_a_wrapper_or_keyword_is_detected(command: str) -> None:
    assert len(push_gate.find_pushes(command, Path("/repo"))) == 1


# --- 27: non-ASCII paths --------------------------------------------------------
def test_non_ascii_paths_are_not_quoted_by_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    (repo / "caf\u00e9.kt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    monkeypatch.chdir(repo)
    assert pre_commit.staged_files() == [("A", "caf\u00e9.kt")]
    _git(repo, "commit", "-q", "-m", "two")
    assert pre_push.changed_files("HEAD~1...HEAD") == [("A", "caf\u00e9.kt")]


# --- 28: a deletion publishes nothing -------------------------------------------
def test_a_delete_only_push_passes_a_stale_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _graph_repo(tmp_path, drift=True)
    head = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(repo)

    def push(line: str) -> int:
        monkeypatch.setattr(pre_push.sys, "stdin", _Stdin(line))
        return pre_push.main()

    assert push(f"(delete) {ZEROS} refs/heads/old {head}\n") == 0
    assert push(f"refs/heads/main {head} refs/heads/main {ZEROS}\n") == 1


@pytest.mark.parametrize("command", [
    "git push origin --delete old",
    "git push -d origin old",
    "git push origin :old",
    "git push -o ci.skip origin :old :older",
])
def test_the_push_gate_lets_a_delete_only_push_through(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _graph_repo(tmp_path, drift=True)
    monkeypatch.chdir(tmp_path)
    (push,) = push_gate.find_pushes(command, repo)
    assert push.deletes_only
    push_gate._run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", [
    "git push",
    "git push origin main",
    "git push origin main :old",
    "git push -o ci.skip origin main",
])
def test_a_push_with_a_pushed_ref_is_not_delete_only(command: str) -> None:
    (push,) = push_gate.find_pushes(command, Path("/repo"))
    assert not push.deletes_only


# --- 29: a staged rename ---------------------------------------------------------
def test_a_staged_rename_reports_the_old_path_as_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _graph_repo(tmp_path)
    _git(repo, "mv", f"src/{KNOWN}", "src/ui/Renamed.kt")
    monkeypatch.chdir(repo)
    assert pre_commit.staged_files() == [("D", f"src/{KNOWN}"), ("A", "src/ui/Renamed.kt")]


# --- 30: the git hook wrappers skip an interpreter that cannot run ---------------
@pytest.mark.parametrize("hook", ["pre-push", "pre-commit"])
def test_the_wrapper_skips_a_python3_stub_and_uses_the_py_launcher(
    hook: str, tmp_path: Path
) -> None:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran"
    (bin_dir / "python3").write_text("#!/bin/sh\nexit 9\n", encoding="utf-8", newline="\n")
    (bin_dir / "py").write_text(
        f'#!/bin/sh\n[ "$1" = "-c" ] || [ "$2" = "-c" ] && exit 0\necho "$@" > "{marker.as_posix()}"\n',
        encoding="utf-8", newline="\n",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in (hook, "kg_pre_push.py", "kg_pre_commit.py"):
        shutil.copy(GIT_HOOKS / name, hooks / name)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    env.pop("SKIP_KG", None)
    done = subprocess.run(
        [sh, str(hooks / hook)], cwd=tmp_path, env=env, input="", capture_output=True,
        text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").startswith("-3 ")


# --- the server is down: a denial no query can clear ----------------------------
def _gate_output(repo: Path, command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo), "session_id": "s1"}
    monkeypatch.setattr(search_gate.sys, "stdin", _Stdin(json.dumps(data)))
    search_gate.main()
    return capsys.readouterr().out


def test_a_search_is_allowed_with_one_notice_when_the_server_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _graph_repo(tmp_path)
    monkeypatch.setattr(search_gate, "server_answers", lambda: False)
    first = json.loads(_gate_output(repo, "grep -rn thing src", monkeypatch, capsys))
    assert "permissionDecision" not in first["hookSpecificOutput"]
    notice = first["hookSpecificOutput"]["additionalContext"]
    assert "server start" in notice and "kg_cli.py" in notice
    assert _gate_output(repo, "grep -rn other src", monkeypatch, capsys) == ""


def test_a_search_is_still_denied_when_the_server_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _graph_repo(tmp_path)
    monkeypatch.setattr(search_gate, "server_answers", lambda: True)
    assert "deny" in _gate_output(repo, "grep -rn thing src", monkeypatch, capsys)


def test_server_answers_follows_the_health_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = search_gate.load_shim()
    monkeypatch.setenv("CODEBASE_KG_PORT", "47999")
    monkeypatch.setattr(search_gate, "load_shim", lambda: shim)
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.FREE, {}))
    assert search_gate.server_answers() is False
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.OTHER, {}))
    assert search_gate.server_answers() is False
    monkeypatch.setattr(shim, "health", lambda port, timeout=1.0: (shim.OURS, {}))
    assert search_gate.server_answers() is True


def test_server_answers_does_not_probe_an_os_chosen_port(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = search_gate.load_shim()
    monkeypatch.setenv("CODEBASE_KG_PORT", "0")
    monkeypatch.setattr(search_gate, "load_shim", lambda: shim)
    monkeypatch.setattr(shim, "health", lambda *a, **k: pytest.fail("port 0 has no address to probe"))
    assert search_gate.server_answers() is True


# --- covers outranks the ignored directories in the hooks ----------------------
COVERS = ["app/src/**/*.kt", ".githooks/*"]


def test_reaches_mapped_code_admits_a_covered_ignored_dir(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "src" / "main" / "build"
    nested.mkdir(parents=True)
    (nested / "Config.kt").write_text("x", encoding="utf-8")
    proj = tmp_path.resolve()
    assert not cfg_mod.reaches_mapped_code(nested, proj, "")
    assert cfg_mod.reaches_mapped_code(nested, proj, "", COVERS)
    assert cfg_mod.reaches_mapped_code(nested / "Config.kt", proj, "", COVERS)
    assert not cfg_mod.reaches_mapped_code(proj / "app" / "build", proj, "", COVERS)
    assert not cfg_mod.reaches_mapped_code(proj / "node_modules", proj, "", ["**/*.py"])


def test_reaches_mapped_code_matches_covers_relative_to_root(tmp_path: Path) -> None:
    proj = tmp_path.resolve()
    assert cfg_mod.reaches_mapped_code(proj / "app" / "src" / "x" / "build", proj, "app", ["src/**/*.kt"])
    assert not cfg_mod.reaches_mapped_code(proj / "app" / "build", proj, "app", ["src/**/*.kt"])
    assert not cfg_mod.reaches_mapped_code(proj / "docs", proj, "app", ["src/**/*.kt"])


def test_a_shell_search_of_a_covered_build_dir_is_gated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / "src" / "main" / "build").mkdir(parents=True)
    scoped = repo.resolve()
    command = "grep -rn thing app/src/main/build"
    assert search_gate.shell_search_is_gated(command, scoped) is False
    assert search_gate.shell_search_is_gated(command, scoped, None, "", COVERS) is True


def test_the_search_gate_reads_covers_from_the_graph(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = "app/src/main/build/Config.kt"
    (repo / Path(nested).parent).mkdir(parents=True)
    (repo / nested).write_text("class C", encoding="utf-8")
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="x", root="", generated="2026-07-30", covers=["app/src/**/*.kt"]),
        [Node(id="c", kind="K", anchors=[Anchor(nested, "C")])],
        source_root=repo,
    )
    graph = cfg_mod.find_graph(repo.resolve(), dict(cfg_mod.DEFAULTS))
    assert graph is not None
    assert cfg_mod.parse_patterns(cfg_mod.graph_meta(graph, "covers")) == ["app/src/**/*.kt"]
    gated = search_gate._graph_if_gated(
        "Grep",
        {"pattern": "C", "path": "app/src/main/build"},
        repo.resolve(),
        dict(cfg_mod.DEFAULTS),
    )
    assert gated == graph


def test_post_edit_hook_nudges_for_a_covered_file_in_a_build_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / "src" / "build").mkdir(parents=True)
    (repo / "app" / "src" / "Known.kt").write_text("class K", encoding="utf-8")
    edited = repo / "app" / "src" / "build" / "Config.kt"
    edited.write_text("class C", encoding="utf-8")
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="x", root="", generated="2026-07-30", covers=["app/src/**/*.kt"]),
        [Node(id="k", kind="K", anchors=[Anchor("app/src/Known.kt", "K")])],
        source_root=repo,
    )
    payload = json.dumps({
        "tool_name": "Edit", "tool_input": {"file_path": str(edited)}, "cwd": str(repo),
    })
    monkeypatch.setattr(post_edit.sys, "stdin", _Stdin(payload))
    post_edit.main()
    out = capsys.readouterr().out
    assert "app/src/build/Config.kt" in out

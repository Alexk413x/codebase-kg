"""The SessionStart notice — the one thing that sees an unwired clone.

`core.hooksPath` and the `diff.codegraph.*` settings live in `.git/config`,
which git never clones. A repo can commit the checkers, the wrappers and the
`.gitattributes` line and still hand every fresh checkout inert hooks and
"Binary files differ", with no error anywhere. This hook notices, says one line,
and — the property these tests exist for — **wires nothing**. Its one write to
git config moves the release an installed diff driver pins.

The silence cases matter as much as the message: a hook that speaks in a repo
that made a different choice is a nag, and gets turned off.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
GIT_HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"
sys.path.insert(0, str(HOOKS))

import kg_session_start as hook

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)},
    )
    return proc.stdout.strip()


def _graph(repo: Path) -> None:
    (repo / "knowledge").mkdir(parents=True, exist_ok=True)
    build(
        repo / "knowledge" / "code_graph.db",
        Meta(codebase="t", root="src", generated="2026-08-01"),
        [Node(id="a", kind="Domain", description="A.", anchors=[Anchor("A.py", "A")],
              edges=[], section="S")],
    )


def _vendor(repo: Path, into: str = ".githooks") -> Path:
    d = repo / into
    d.mkdir(parents=True, exist_ok=True)
    for name in ("kg_pre_commit.py", "kg_pre_push.py", "pre-commit", "pre-push",
                 "install.sh"):
        shutil.copy(GIT_HOOKS / name, d / name)
    return d


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLAUDE_PROJECT_DIR outranks the payload cwd; a developer's real value would
    # point every test at their own repo.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with a graph and the checkers vendored, nothing configured."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _graph(r)
    _vendor(r)
    return r


def test_fires_on_an_unwired_clone(repo: Path) -> None:
    msg = hook.advice(repo)
    assert msg is not None
    assert "install.sh" in msg


def test_names_the_command_for_the_configured_hooks_dir(tmp_path: Path) -> None:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _graph(r)
    _vendor(r, into="tools/hooks")
    _git(r, "config", "core.hooksPath", "tools/hooks")
    msg = hook.advice(r)
    assert msg is not None and "sh tools/hooks/install.sh" in msg


def test_falls_back_to_the_command_when_no_installer_is_vendored(repo: Path) -> None:
    (repo / ".githooks" / "install.sh").unlink()
    msg = hook.advice(repo)
    assert msg is not None and "/codebase-kg:setup" in msg


def test_silent_outside_a_git_work_tree(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _graph(plain)
    _vendor(plain)
    assert hook.advice(plain) is None


def test_silent_in_a_repo_with_no_graph(tmp_path: Path) -> None:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _vendor(r)
    assert hook.advice(r) is None


def test_silent_when_the_checkers_are_not_vendored(tmp_path: Path) -> None:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _graph(r)
    assert hook.advice(r) is None


def test_silent_when_fully_wired(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "config", "diff.codegraph.textconv", "uvx --quiet --from x codebase-kg-export")
    assert hook.advice(repo) is None


def test_fires_when_only_the_textconv_half_is_missing(repo: Path) -> None:
    """The half that setup used to verify in the shell that had just set it."""
    _git(repo, "config", "core.hooksPath", ".githooks")
    msg = hook.advice(repo)
    assert msg is not None and "graph diffs" in msg


def test_never_nags_a_repo_whose_hooks_point_elsewhere(repo: Path) -> None:
    """A deliberate choice. Suggesting a clobber is worse than saying nothing."""
    other = repo / "other-hooks"
    other.mkdir()
    _git(repo, "config", "core.hooksPath", "other-hooks")
    assert hook.advice(repo) is None


def test_mutates_nothing(repo: Path) -> None:
    """The hard constraint. Git leaves .git/config out of a clone so that cloning
    cannot cause code to run; a hook that wired core.hooksPath would undo that."""
    before = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert hook.advice(repo) is not None
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == before
    assert _git(repo, "config", "--get", "core.hooksPath") == ""
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == ""


def test_the_hook_source_runs_no_write_command() -> None:
    """Pins the constraint against the source, not just against one run."""
    src = (HOOKS / "kg_session_start.py").read_text(encoding="utf-8")
    assert "--get" in src
    for banned in ("update-index", "check-ignore --", "config --replace"):
        assert banned not in src
    # Every `_git` call is a read. The one write is `repin`, which sets the
    # textconv of a driver that is already installed and nothing else.
    assert src.count('_git(') >= 3
    assert src.count('"config", "diff.codegraph.textconv", moved') == 1
    assert '"core.hooksPath",' not in src.replace('"--get", "core.hooksPath"', "")


def test_main_is_silent_on_a_malformed_payload(capsys: pytest.CaptureFixture[str]) -> None:
    class _Stdin:
        def read(self) -> str:
            return "not json"

    old = sys.stdin
    sys.stdin = _Stdin()  # type: ignore[assignment]
    try:
        hook.main()
    finally:
        sys.stdin = old
    assert capsys.readouterr().out == ""


def test_skip_kg_silences_it(repo: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SKIP_KG", "1")

    class _Stdin:
        def read(self) -> str:
            return f'{{"cwd": {str(repo)!r}}}'

    old = sys.stdin
    sys.stdin = _Stdin()  # type: ignore[assignment]
    try:
        hook.main()
    finally:
        sys.stdin = old
    assert capsys.readouterr().out == ""


# --- a stale graph-diff pin ---------------------------------------------------
URL = "uvx --quiet --from \"git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v{}#subdirectory=mcp\" codebase-kg-export"


def test_a_pin_older_than_the_plugin_is_reported() -> None:
    why = hook.stale_pin(URL.format("0.5.3"), "0.8.2")
    assert why is not None and "0.5.3" in why and "0.8.2" in why


@pytest.mark.parametrize("pinned", ["0.8.2", "0.9.0"])
def test_a_current_or_newer_pin_is_silent(pinned: str) -> None:
    assert hook.stale_pin(URL.format(pinned), "0.8.2") is None


def test_a_pin_to_a_missing_plugin_folder_is_reported(tmp_path: Path) -> None:
    gone = (tmp_path / "cache" / "codebase-kg" / "codebase-kg" / "0.2.3" / "mcp" / "src").as_posix()
    textconv = f"python -c \"import sys; sys.path.insert(0, '{gone}'); from codebase_kg.export import main; main()\""
    why = hook.stale_pin(textconv, "0.8.2")
    assert why is not None and "0.2.3" in why


def test_a_pin_to_an_existing_plugin_folder_is_silent(tmp_path: Path) -> None:
    here = tmp_path / "codebase-kg" / "mcp"
    here.mkdir(parents=True)
    assert hook.stale_pin(f'uvx --from "{here.as_posix()}" codebase-kg-export', "0.8.2") is None


def test_someone_elses_driver_is_not_ours_to_judge() -> None:
    assert hook.stale_pin("my-own-exporter --json", "0.8.2") is None


def _wire(repo: Path, textconv: str) -> None:
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "config", "diff.codegraph.textconv", textconv)


def test_a_stale_pin_moves_to_the_installed_release(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(hook, "plugin_version", lambda: "0.8.2")
    monkeypatch.setattr(hook, "tag_published", lambda url, v: asked.append((url, v)) or True)
    _wire(repo, URL.format("0.1.0"))
    msg = hook.advice(repo)
    assert msg is not None and "0.1.0" in msg and "0.8.2" in msg and "setup" not in msg
    assert asked == [("https://github.com/Alexk413x/codebase-kg.git", "0.8.2")]
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == URL.format("0.8.2")
    assert hook.advice(repo) is None  # current now, so the next session is silent


def test_a_stale_pin_stays_when_the_new_tag_is_not_published(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver that names a missing tag makes `git diff` on the graph fail."""
    monkeypatch.setattr(hook, "plugin_version", lambda: "0.8.2")
    monkeypatch.setattr(hook, "tag_published", lambda url, v: False)
    _wire(repo, URL.format("0.7.0"))
    assert hook.advice(repo) is None
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == URL.format("0.7.0")


def test_an_unmoved_pin_that_cannot_read_the_graph_is_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is right only while the old exporter still reads the graph."""
    monkeypatch.setattr(hook, "plugin_version", lambda: "0.8.2")
    monkeypatch.setattr(hook, "tag_published", lambda url, v: False)
    schema = int(hook.graph_meta(repo / "knowledge" / "code_graph.db", "schema_version"))
    monkeypatch.setattr(hook, "SCHEMA_SINCE", {schema: "0.7.0"})
    _wire(repo, URL.format("0.6.1"))
    msg = hook.advice(repo)
    assert msg is not None and "0.6.1" in msg and "0.7.0" in msg and f"schema v{schema}" in msg
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == URL.format("0.6.1")
    _wire(repo, URL.format("0.7.0"))
    assert hook.advice(repo) is None


def test_the_newest_schema_names_its_first_release() -> None:
    """A schema bump without a row here leaves a broken pin unreported."""
    from codebase_kg.schema import MIN_READABLE_VERSION, SCHEMA_VERSION

    assert set(range(MIN_READABLE_VERSION + 1, SCHEMA_VERSION + 1)) <= set(hook.SCHEMA_SINCE)


def test_a_newer_pin_is_never_moved_back(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "tag_published", lambda url, v: True)
    assert hook.repin(repo, URL.format("0.9.0"), "0.8.2") is None
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == ""


def test_the_notice_names_setup_for_a_missing_plugin_folder(repo: Path, tmp_path: Path) -> None:
    gone = (tmp_path / "gone" / "codebase-kg" / "kg_cli.py").as_posix()
    textconv = f'python3 "{gone}" textconv'
    _wire(repo, textconv)
    msg = hook.advice(repo)
    assert msg is not None and "/codebase-kg:setup" in msg
    assert _git(repo, "config", "--get", "diff.codegraph.textconv") == textconv


def test_an_unreachable_remote_is_not_a_published_tag(tmp_path: Path) -> None:
    assert hook.tag_published((tmp_path / "no-such-repo").as_uri(), "0.8.2") is False


def test_a_stale_pin_on_an_offline_machine_stays_inside_the_hook_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hooks.json allows 10 s. Every git call, however slow, draws on one budget."""
    clock = [1000.0]
    calls: list[float] = []

    def slow(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        limit = float(kwargs["timeout"])  # type: ignore[arg-type]
        calls.append(limit)
        clock[0] += limit
        raise subprocess.TimeoutExpired(command, limit)

    _wire(repo, URL.format("0.1.0"))
    monkeypatch.setattr(hook, "plugin_version", lambda: "0.8.2")
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook, "_deadline", clock[0] + hook.HOOK_BUDGET)
    monkeypatch.setattr(hook.subprocess, "run", slow)
    assert hook.advice(repo) is None
    assert calls and sum(calls) <= hook.HOOK_BUDGET < 10
    assert clock[0] - 1000.0 < 10


def test_every_git_call_is_skipped_once_the_budget_is_spent(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "_deadline", hook.time.monotonic() - 1)
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: pytest.fail("a git call ran"))
    assert hook._git(repo, "status") == ""
    assert hook.tag_published("https://example.invalid/x.git", "1.0.0") is False
    assert hook.repin(repo, URL.format("0.1.0"), "0.8.2") is None


def test_a_url_that_looks_like_an_option_is_not_read_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(hook.subprocess, "run", run)
    url = "--upload-pack=touch x"
    assert hook.tag_published(url, "0.8.2") is False
    (command,) = seen
    assert command[command.index(url) - 1] == "--"

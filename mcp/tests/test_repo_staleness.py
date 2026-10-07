"""The standing total: staleness asked about the whole repo, not a change set.

A consumer repo carried 47 stale files for months with every check passing. The
tool was not wrong. Both hooks scope to a change set, which is right for
per-commit noise and wrong for a standing gap — a file that drifts and is never
re-derived is reported once, in the commit that touched it, and never again.
`kg_validate` knew the repo-wide number and nothing routine ran it.

These tests pin the four things that fix: one comparison rule shared by every
caller, the total surfaced where an agent orients (`kg_stats`), one line at
commit, and a push that stops for any stale mapped file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codebase_kg import edits, staleness, tools
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build, content_sha

HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"
sys.path.insert(0, str(HOOKS))

import kg_pre_commit as c  # noqa: E402
import kg_pre_push as g  # noqa: E402

KNOWN = "ui/Known.kt"
RANKER = "domain/Ranker.kt"
REFS = "refs/heads/m aaa refs/heads/m bbb\n"


# --- the one rule ------------------------------------------------------------
def test_a_differing_digest_is_stale() -> None:
    split = staleness.classify([KNOWN], {KNOWN: "a" * 64}, {KNOWN: "b" * 64})
    assert split == ([KNOWN], [], [])


def test_a_matching_digest_is_clean() -> None:
    assert staleness.classify([KNOWN], {KNOWN: "a" * 64}, {KNOWN: "a" * 64}) == ([], [], [])


def test_no_baseline_is_unbaselined_never_stale() -> None:
    """SCHEMA.md §6.3: absent evidence reads as "no baseline", never "changed"."""
    assert staleness.classify([KNOWN], {}, {KNOWN: "b" * 64}) == ([], [KNOWN], [])


def test_an_unreadable_file_makes_no_claim_either_way() -> None:
    """Unreadable outranks unbaselined. A file nobody could digest is not
    evidence of drift and is not evidence of a missing baseline — reporting it
    as either would put a number on something never looked at."""
    assert staleness.classify([KNOWN], {KNOWN: "a" * 64}, {}) == ([], [], [KNOWN])
    assert staleness.classify([KNOWN], {}, {}) == ([], [], [KNOWN])


def test_the_split_is_sorted_and_deduplicated() -> None:
    split = staleness.classify(
        [RANKER, KNOWN, KNOWN], {KNOWN: "a" * 64, RANKER: "a" * 64}, {KNOWN: "b" * 64, RANKER: "b" * 64}
    )
    assert split.stale == [RANKER, KNOWN]  # 'domain/…' sorts before 'ui/…'


# --- kg_stats ----------------------------------------------------------------
def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A two-file source tree with a graph built against it. Returns (base, db)."""
    src = tmp_path / "src"
    (src / "ui").mkdir(parents=True)
    (src / "domain").mkdir(parents=True)
    (src / "ui" / "Known.kt").write_text("class Known { fun a() {} }", encoding="utf-8")
    (src / "domain" / "Ranker.kt").write_text("class Ranker { fun b() {} }", encoding="utf-8")
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [
            Node(id="known", kind="K", anchors=[Anchor(KNOWN, "Known")]),
            Node(id="ranker", kind="K", anchors=[Anchor(RANKER, "Ranker")]),
        ],
        source_root=src,
    )
    return src, db


def _stats(db: Path, base: Path) -> dict[str, object]:
    graph = CodeGraph(db)
    try:
        return tools.kg_stats(graph, str(base))
    finally:
        graph.close()


def test_stats_reports_a_clean_graph_as_clean(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    s = _stats(db, base)["staleness"]
    assert isinstance(s, dict)
    assert s["checked"] is True
    assert s["stale_files"] == 0 and s["stale_nodes"] == 0
    assert s["hint"] == ""


def test_stats_reports_stale_files_and_the_nodes_that_describe_them(tmp_path: Path) -> None:
    """The number that was missing. This is where an agent orients first."""
    base, db = _repo(tmp_path)
    (base / "ui" / "Known.kt").write_text("class Known { fun gutted() {} }", encoding="utf-8")

    s = _stats(db, base)["staleness"]
    assert isinstance(s, dict)
    assert s["stale_files"] == 1
    assert s["files"] == [KNOWN]
    assert s["stale_nodes"] == 1 and s["nodes"] == ["known"]
    assert "/codebase-kg:audit" in str(s["hint"])


def test_stats_reports_an_unbaselined_file_separately(tmp_path: Path) -> None:
    base, db = _repo(tmp_path)
    graph = CodeGraph(db)
    try:
        carried = {k: v for k, v in graph.sources().items() if k != KNOWN}
        nodes = graph.all_nodes()
        meta = graph.meta
    finally:
        graph.close()
    build(db, meta, nodes, sources=carried)  # no source_root -> the gap survives

    s = _stats(db, base)["staleness"]
    assert isinstance(s, dict)
    assert s["stale_files"] == 0 and s["unbaselined_files"] == 1


def test_stats_says_it_could_not_look_rather_than_reporting_zero(tmp_path: Path) -> None:
    """"Nothing drifted" and "I could not find the source" are different answers,
    and a bare count conflates them into the reassuring one."""
    db = tmp_path / "knowledge" / "code_graph.db"
    build(db, Meta(codebase="x"), [Node(id="a", kind="K", anchors=[Anchor("gone/X.kt")])])
    graph = CodeGraph(db)
    try:
        s = tools.kg_stats(graph)["staleness"]
    finally:
        graph.close()
    assert isinstance(s, dict)
    assert s["checked"] is False and s["stale_files"] == 0


def test_stats_and_validate_report_the_same_block(tmp_path: Path) -> None:
    """Two tools that both report drift must not report different numbers —
    which is the whole reason the comparison moved into one helper."""
    base, db = _repo(tmp_path)
    (base / "domain" / "Ranker.kt").write_text("class Ranker { fun c() {} }", encoding="utf-8")

    graph = CodeGraph(db)
    try:
        assert tools.kg_stats(graph, str(base))["staleness"] == (
            tools.kg_validate(graph, None, str(base))["staleness"]
        )
    finally:
        graph.close()


def test_validate_still_does_not_fold_staleness_into_ok(tmp_path: Path) -> None:
    """`ok` means broken, not "someone touched a file". Adding the repo-wide
    block must not quietly change what a green validate claims."""
    base, db = _repo(tmp_path)
    (base / "domain" / "Ranker.kt").write_text("class Ranker { fun c() {} }", encoding="utf-8")
    graph = CodeGraph(db)
    try:
        v = tools.kg_validate(graph, None, str(base))
    finally:
        graph.close()
    assert v["ok"] is True
    assert v["staleness"]["stale_files"] == 1  # type: ignore[index]


# --- the hooks ---------------------------------------------------------------
class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text


def _fake_git(repo: Path, diff_output: str = "", answers=None):
    """`answers(args)` returns canned stdout (or None) for the calls the
    auto-refresh makes: `rev-parse HEAD`, `status --porcelain`."""

    def fake_git(*args: str) -> str:
        if answers is not None:
            out = answers(args)
            if out is not None:
                return out
        if args[0] == "rev-parse" and "--show-toplevel" in args:
            return str(repo) + "\n"
        if args[0] == "diff":
            return diff_output + "\n" if diff_output else ""
        return ""

    return fake_git


def _fake_blobs(by_path: dict[str, str]):
    """Serve `<rev>:<repo-relative-path>` specs from a path -> digest map.

    Stands in for `git cat-file --batch` at the one seam that touches git, so the
    root translation, the rev fallback and the comparison above it are all real.
    """

    def blob_digests(specs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for spec in specs:
            path = spec.split(":", 1)[1]
            if path in by_path:
                out[spec] = by_path[path]
        return out

    return blob_digests


def _tree_digests(base: Path, drifted: tuple[str, ...] = ()) -> dict[str, str]:
    """What git would report for each anchored file, with `drifted` altered."""
    out: dict[str, str] = {}
    for rel in (KNOWN, RANKER):
        data = (base / rel).read_bytes()
        if rel in drifted:
            data += b"\n// rewritten since the graph was built\n"
        out[f"src/{rel}"] = content_sha(data)
    return out


@pytest.fixture
def hook_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("KG_STALE_ACK", raising=False)
    monkeypatch.delenv("SKIP_KG", raising=False)
    monkeypatch.delenv("KG_REFRESHING", raising=False)
    monkeypatch.setenv("KG_AUTO_REFRESH", "0")
    monkeypatch.setattr(g.sys, "stdin", _Stdin(REFS))
    _repo(tmp_path)
    return tmp_path


def test_push_is_silent_when_the_repo_is_clean(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M\tsrc/ui/Known.kt"))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src")))
    assert g.main() == 0
    assert capsys.readouterr().err == ""


def test_push_reports_the_repo_wide_total(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M\tsrc/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (KNOWN,)))
    )
    g.main()
    err = capsys.readouterr().err
    assert "Repo-wide: 1 mapped file(s) and 1 node(s)" in err


def test_drift_this_push_introduces_blocks(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The gate covers every stale file, including one this push touches. A push
    publishes the code, and a graph that lags it stays wrong for every reader."""
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M	src/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (KNOWN,)))
    )
    assert g.main() == 1
    err = capsys.readouterr().err
    assert "PUSH BLOCKED" in err
    assert f"src/{KNOWN}" in err
    assert "KG_STALE_ACK=1" in err


def test_a_stale_file_this_push_did_not_touch_blocks(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Drift left behind in some earlier commit, which no change set will ever
    mention again."""
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M	src/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (RANKER,)))
    )
    assert g.main() == 1
    err = capsys.readouterr().err
    assert "PUSH BLOCKED" in err
    # Repo-relative, like every other path in the report — anchors are stored
    # relative to the graph's `root` and printing them that way reads as a
    # different file from the one the findings above name.
    assert f"src/{RANKER}" in err
    assert "KG_STALE_ACK=1" in err


def test_the_block_lists_touched_and_untouched_stale_files(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M	src/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (KNOWN, RANKER)))
    )
    assert g.main() == 1
    err = capsys.readouterr().err
    assert f"src/{KNOWN}" in err
    assert f"src/{RANKER}" in err
    assert "KG_STALE_ACK=2" in err


def test_a_blocked_push_does_not_also_claim_to_be_going_through(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The change-set findings stay advisory, but a header promising the push is
    going through three lines above one saying it is not is the same defect the
    `action` parameter was added to fix."""
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "A\tsrc/ui/Brand.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (RANKER,)))
    )
    assert g.main() == 1
    err = capsys.readouterr().err
    assert "Brand.kt" in err  # the advisory finding is still reported
    assert "is going through" not in err
    assert "Nothing is blocked" not in err


def test_the_acknowledgement_has_to_name_the_total_count(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ack meaning "yes, whatever the number" is --no-verify in a different
    spelling: set once and the gate is off for every file that rots afterwards.
    The count covers every stale file, touched by the push or not."""
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M	src/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (KNOWN, RANKER)))
    )
    monkeypatch.setenv("KG_STALE_ACK", "7")
    assert g.main() == 1
    monkeypatch.setenv("KG_STALE_ACK", "1")
    assert g.main() == 1
    monkeypatch.setenv("KG_STALE_ACK", "2")
    assert g.main() == 0


def test_an_acknowledgement_releases_drift_this_push_touches(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M	src/ui/Known.kt"))
    monkeypatch.setattr(
        g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (KNOWN,)))
    )
    assert g.main() == 1
    monkeypatch.setenv("KG_STALE_ACK", "1")
    assert g.main() == 0


def test_a_rebaseline_upsert_releases_the_block(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ranker = hook_repo / "src" / RANKER
    ranker.write_text("class Ranker { fun rewritten() {} }", encoding="utf-8")
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M\tsrc/ui/Known.kt"))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src")))
    assert g.main() == 1

    edits.upsert_node(
        hook_repo / "knowledge" / "code_graph.db",
        [{"id": "ranker", "description": "Ranks by the rewritten rule.", "rebaseline": True}],
    )
    capsys.readouterr()
    assert g.main() == 0
    assert "PUSH BLOCKED" not in capsys.readouterr().err


# --- the automatic refresh ---------------------------------------------------
class _Refresh:
    """Arms the hook for an auto-refresh with `claude` and the commit mocked.

    `refreshed` flips when the fake run happens: the source blobs then match the
    graph and the graph file shows as modified, as it would after a real refresh.
    """

    def __init__(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.repo = repo
        self.refreshed = False
        self.runs: list[str] = []
        self.commits: list[str] = []
        self.run_result: str | None = None
        self.leaves_stale = False
        self.changes_graph = True
        self.graph_dirty = False
        self.head = "aaa"
        self.claude: str | None = "/usr/bin/claude"
        monkeypatch.delenv("KG_AUTO_REFRESH", raising=False)
        monkeypatch.setattr(g.shutil, "which", lambda _name: self.claude)
        monkeypatch.setattr(g, "_git", _fake_git(repo, "M\tsrc/ui/Known.kt", self._answer))
        monkeypatch.setattr(g, "blob_digests", self._blobs)
        monkeypatch.setattr(g, "_run_claude", self._run_claude)
        monkeypatch.setattr(g, "_commit_graph", self._commit_graph)

    def _answer(self, args: tuple[str, ...]) -> str | None:
        if args[:2] == ("rev-parse", "HEAD"):
            return self.head + "\n"
        if args[:2] == ("rev-parse", "--short"):
            return "abc1234\n"
        if args[:2] == ("status", "--porcelain"):
            dirty = self.graph_dirty or (self.refreshed and self.changes_graph)
            return " M knowledge/code_graph.db\n" if dirty else ""
        return None

    def _blobs(self, specs: list[str]) -> dict[str, str]:
        drifted = () if self.refreshed and not self.leaves_stale else (KNOWN,)
        return _fake_blobs(_tree_digests(self.repo / "src", drifted))(specs)

    def _run_claude(self, claude: str, repo: Path, stale: list[str]) -> str | None:
        self.runs.append(claude)
        self.refreshed = True
        return self.run_result

    def _commit_graph(self, graph_rel: str) -> str | None:
        self.commits.append(graph_rel)
        return "abc1234"


@pytest.fixture
def refresh(hook_repo: Path, monkeypatch: pytest.MonkeyPatch) -> _Refresh:
    return _Refresh(hook_repo, monkeypatch)


def test_a_blocked_push_runs_the_refresh_and_commits_the_graph(
    refresh: _Refresh, capsys: pytest.CaptureFixture
) -> None:
    assert g.main() == 1
    err = capsys.readouterr().err
    assert refresh.runs == ["/usr/bin/claude"]
    assert refresh.commits == ["knowledge/code_graph.db"]
    assert '`claude -p "/codebase-kg:refresh"`' in err
    assert "KG_AUTO_REFRESH=0" in err
    assert "refreshed and committed as abc1234" in err
    assert "then run git push again" in err
    assert "PUSH BLOCKED" not in err


def test_a_failed_run_blocks_and_commits_nothing(
    refresh: _Refresh, capsys: pytest.CaptureFixture
) -> None:
    refresh.run_result = "claude exited with status 2"
    assert g.main() == 1
    err = capsys.readouterr().err
    assert refresh.runs and refresh.commits == []
    assert "Auto-refresh failed: claude exited with status 2" in err
    assert "left uncommitted" in err
    assert "PUSH BLOCKED" in err
    assert "KG_STALE_ACK=1" in err


def test_a_refresh_that_leaves_files_stale_blocks_and_commits_nothing(
    refresh: _Refresh, capsys: pytest.CaptureFixture
) -> None:
    refresh.leaves_stale = True
    assert g.main() == 1
    err = capsys.readouterr().err
    assert refresh.commits == []
    assert "1 mapped file(s) are still stale after the refresh" in err
    assert "PUSH BLOCKED" in err


def test_a_refresh_that_changes_nothing_blocks(
    refresh: _Refresh, capsys: pytest.CaptureFixture
) -> None:
    refresh.changes_graph = False
    assert g.main() == 1
    err = capsys.readouterr().err
    assert refresh.commits == []
    assert "did not change the graph" in err
    assert "PUSH BLOCKED" in err


def test_a_failed_commit_blocks(
    refresh: _Refresh, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g, "_commit_graph", lambda _rel: None)
    assert g.main() == 1
    err = capsys.readouterr().err
    assert "git could not commit the refreshed graph" in err
    assert "PUSH BLOCKED" in err


def test_a_raising_refresh_blocks_rather_than_passing(
    refresh: _Refresh, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(g, "_run_claude", boom)
    assert g.main() == 1
    err = capsys.readouterr().err
    assert "the refresh raised: kaboom" in err
    assert "PUSH BLOCKED" in err


@pytest.mark.parametrize(
    "setup",
    ["no_claude", "disabled", "recursion", "head_not_pushed", "graph_dirty"],
)
def test_each_precondition_skips_the_refresh(
    refresh: _Refresh,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    setup: str,
) -> None:
    if setup == "no_claude":
        refresh.claude = None
    elif setup == "disabled":
        monkeypatch.setenv("KG_AUTO_REFRESH", "0")
    elif setup == "recursion":
        monkeypatch.setenv("KG_REFRESHING", "1")
    elif setup == "head_not_pushed":
        refresh.head = "zzz"
    else:
        refresh.graph_dirty = True
    assert g.main() == 1
    err = capsys.readouterr().err
    assert refresh.runs == [] and refresh.commits == []
    assert "Auto-refresh failed" not in err
    assert "PUSH BLOCKED" in err


def test_an_acknowledged_push_does_not_refresh(
    refresh: _Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_STALE_ACK", "1")
    assert g.main() == 0
    assert refresh.runs == []


def test_skip_kg_does_not_refresh(
    refresh: _Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKIP_KG", "1")
    assert g.main() == 0
    assert refresh.runs == []


def test_a_clean_repo_does_not_refresh(
    refresh: _Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(refresh.repo / "src")))
    assert g.main() == 0
    assert refresh.runs == []


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_run_claude_runs_the_skill_headless_with_a_narrow_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return _Completed()

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert g._run_claude("claude", tmp_path, ["src/a.kt"]) is None
    cmd, kwargs = seen["cmd"], seen["kwargs"]
    assert isinstance(cmd, list) and isinstance(kwargs, dict)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[2].startswith("/codebase-kg:refresh ")
    assert cmd[2].endswith("Stale mapped files:\n- src/a.kt")
    assert "--dangerously-skip-permissions" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__plugin_codebase-kg_codebase-kg__kg_upsert_node" in allowed
    assert "mcp__codebase-kg__kg_validate" in allowed
    assert not any(t.startswith("Bash") for t in allowed)
    assert not any(t in ("Write", "Edit", "Read") for t in allowed)
    assert not any("kg_cli" in t or t.startswith(("Write(", "Edit(")) for t in allowed)
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is g.subprocess.DEVNULL
    assert kwargs["timeout"] == g.REFRESH_TIMEOUT
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["KG_REFRESHING"] == "1"
    assert "PATH" in env
    assert "GIT_DIR" not in env and "GITHUB_TOKEN" not in env


def test_run_claude_reports_a_nonzero_exit_and_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        g.subprocess, "run", lambda *a, **k: _Completed(3, stderr="line1\nauth required\n")
    )
    reason = g._run_claude("claude", tmp_path, ["src/a.kt"])
    assert reason is not None and "status 3" in reason and "auth required" in reason

    def hang(*_a: object, **_k: object) -> None:
        raise g.subprocess.TimeoutExpired("claude", g.REFRESH_TIMEOUT)

    monkeypatch.setattr(g.subprocess, "run", hang)
    reason = g._run_claude("claude", tmp_path, ["src/a.kt"])
    assert reason is not None and "did not finish" in reason


def test_commit_graph_adds_and_commits_only_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        calls.append((cmd, kwargs))
        return _Completed()

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    monkeypatch.setattr(g, "_git", lambda *a: "abc1234\n")
    assert g._commit_graph("knowledge/code_graph.db") == "abc1234"
    assert calls[0][0] == ["git", "add", "--", "knowledge/code_graph.db"]
    assert calls[1][0][:4] == ["git", "commit", "-m", "Refresh the code graph"]
    assert calls[1][0][-1] == "knowledge/code_graph.db"
    assert all(kw["env"]["KG_REFRESHING"] == "1" for _c, kw in calls)  # type: ignore[index]


def test_skip_kg_silences_the_push_check(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("SKIP_KG", "1")
    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M\tsrc/ui/Known.kt"))
    assert g.main() == 0
    assert capsys.readouterr().err == ""


def test_a_graph_with_no_baselines_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent evidence must not become a gate. A graph built with no source tree
    in reach records no digests, and a hook that blocked on that would block
    every push in the repo for a reason nobody could act on."""
    monkeypatch.delenv("KG_STALE_ACK", raising=False)
    monkeypatch.setattr(g.sys, "stdin", _Stdin(REFS))
    db = tmp_path / "knowledge" / "code_graph.db"
    build(
        db,
        Meta(codebase="x", root="src", generated="2026-07-30"),
        [Node(id="known", kind="K", anchors=[Anchor(KNOWN, "Known")])],
    )
    monkeypatch.setattr(g, "_git", _fake_git(tmp_path, "M\tsrc/ui/Known.kt"))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs({f"src/{KNOWN}": "c" * 64}))
    assert g.main() == 0


def test_an_error_in_the_check_is_never_a_block(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Now that the wrapper propagates the status, a bug in here could fail every
    push in a repo. It reports itself and exits 0 instead."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(g, "_git", _fake_git(hook_repo, "M\tsrc/ui/Known.kt"))
    monkeypatch.setattr(g, "repo_staleness", boom)
    assert g.main() == 0
    assert "did not run" in capsys.readouterr().err


# --- pre-commit: one line ----------------------------------------------------
def test_commit_prints_the_standing_total_as_one_line(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """One line is the whole budget. Change-set scoping is correct at commit
    time and must not become noisy, but the stale files cannot stay invisible at
    both hooks."""
    monkeypatch.setattr(c, "_git", _fake_git(hook_repo))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (RANKER,))))
    assert c.main() == 0
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "Repo-wide: 1 mapped file(s) and 1 node(s)" in lines[0]


def test_commit_is_silent_when_the_repo_is_clean(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(c, "_git", _fake_git(hook_repo))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src")))
    assert c.main() == 0
    assert capsys.readouterr().err == ""


def test_commit_still_never_blocks(
    hook_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(c, "_git", _fake_git(hook_repo, "A\tsrc/ui/Brand.kt"))
    monkeypatch.setattr(g, "blob_digests", _fake_blobs(_tree_digests(hook_repo / "src", (RANKER,))))
    assert c.main() == 0


# --- the wrappers ------------------------------------------------------------
# A checker that returns 1 gates nothing if the shell around it throws the status
# away, and the discarding form is what `pre-push` shipped for three releases.
# Reading the file is the only place that join is visible.
def test_the_push_wrapper_does_not_swallow_the_exit_status() -> None:
    text = (HOOKS / "pre-push").read_text(encoding="utf-8")
    call = [ln for ln in text.splitlines() if "kg_pre_push.py" in ln and not ln.startswith("#")]
    assert call, "the wrapper no longer runs the checker"
    assert not any("|| true" in ln for ln in call)


def test_the_push_wrapper_still_survives_a_missing_interpreter() -> None:
    """Without the status swallowed, a `127` would block every push in a repo
    that cannot run the check at all."""
    text = (HOOKS / "pre-push").read_text(encoding="utf-8")
    assert "command -v python >/dev/null 2>&1" in text
    assert 'else exit 0' in text
    assert '[ -f "$DIR/kg_pre_push.py" ] || exit 0' in text


def test_the_commit_wrapper_still_swallows_it() -> None:
    """Commit-time stays advisory. Only the push gates."""
    text = (HOOKS / "pre-commit").read_text(encoding="utf-8")
    assert any(
        "kg_pre_commit.py" in ln and "|| true" in ln
        for ln in text.splitlines()
        if not ln.startswith("#")
    )

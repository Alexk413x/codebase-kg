"""`hooks/_config.py` — the resolution layer under the post-edit nudge.

`test_hook_parity.py` covers the constants and the frontmatter parser, because
those are the halves that had drifted from the pre-push gate. Everything the
hook actually *calls* at runtime — `load_config`, `find_graph`, `_rows`,
`graph_meta`, `is_anchored`, `is_source_file` — was uncovered, which is how a
file that runs after every edit sat at 64%.

The theme throughout is that this module is best-effort by contract: every
failure has to read as "no nudge", never as an exception, because the caller is
a hook that must not break an edit.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.writer import build

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import _config as c


@pytest.fixture
def graph_db(tmp_path: Path) -> Path:
    db = tmp_path / "knowledge" / "code_graph.db"
    db.parent.mkdir(parents=True)
    build(
        db,
        Meta(codebase="test", root="app/src", generated="2026-08-01"),
        [Node(
            id="feed_ranker", kind="Domain", description="Ranks the feed.",
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=[], section="DOMAIN",
        )],
    )
    return db


# --- project_dir -------------------------------------------------------------
def test_project_dir_prefers_the_claude_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert c.project_dir("/somewhere/else") == tmp_path.resolve()


def test_project_dir_falls_back_to_cwd_then_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert c.project_dir(str(tmp_path)) == tmp_path.resolve()
    assert c.project_dir(None) == Path.cwd()


# --- load_config -------------------------------------------------------------
def test_load_config_returns_defaults_without_a_local_md(tmp_path: Path) -> None:
    assert c.load_config(tmp_path) == c.DEFAULTS


def test_load_config_overlays_only_the_declared_keys(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\nroot: app/src\nnudge_every: 9\n---\n", encoding="utf-8"
    )
    cfg = c.load_config(tmp_path)
    assert cfg["root"] == "app/src"
    assert cfg["nudge_every"] == 9
    assert cfg["exclude_ext"] == c.DEFAULTS["exclude_ext"]  # untouched


def test_load_config_does_not_mutate_the_shared_defaults(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(
        "---\nroot: app/src\n---\n", encoding="utf-8"
    )
    c.load_config(tmp_path)
    assert c.DEFAULTS["root"] == ""  # a module-level dict handed out by reference


@pytest.mark.parametrize("body", [
    "no frontmatter at all",
    "",
    "---\nnot: closed properly",
    "---\n\xff\xfe binary-ish\n---\n",
])
def test_an_unparseable_local_md_falls_back_to_defaults(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text(body, encoding="utf-8")
    cfg = c.load_config(tmp_path)
    assert cfg["graph_path"] == c.DEFAULTS["graph_path"]


def test_an_unreadable_local_md_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "codebase-kg.local.md").write_text("---\n", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert c.load_config(tmp_path) == c.DEFAULTS


# --- find_graph --------------------------------------------------------------
def test_find_graph_resolves_the_default_location(graph_db: Path) -> None:
    assert c.find_graph(graph_db.parent.parent, dict(c.DEFAULTS)) == graph_db.resolve()


def test_find_graph_honours_an_explicit_relative_override(
    tmp_path: Path, graph_db: Path
) -> None:
    moved = tmp_path / "elsewhere" / "g.db"
    moved.parent.mkdir()
    moved.write_bytes(graph_db.read_bytes())
    cfg = {**c.DEFAULTS, "graph_path": "elsewhere/g.db"}
    assert c.find_graph(tmp_path, cfg) == moved.resolve()


def test_find_graph_honours_an_absolute_override(tmp_path: Path, graph_db: Path) -> None:
    cfg = {**c.DEFAULTS, "graph_path": str(graph_db)}
    assert c.find_graph(tmp_path / "unrelated", cfg) == graph_db.resolve()


def test_find_graph_still_reads_the_legacy_kg_path_key(
    tmp_path: Path, graph_db: Path
) -> None:
    cfg = {k: v for k, v in c.DEFAULTS.items() if k != "graph_path"}
    cfg["kg_path"] = str(graph_db)
    assert c.find_graph(tmp_path, cfg) == graph_db.resolve()


def test_find_graph_returns_none_when_absent(tmp_path: Path) -> None:
    # No root fallback: a missing file means "this repo has no graph", and the
    # hook no-ops rather than guessing at some other .db in the tree.
    assert c.find_graph(tmp_path, dict(c.DEFAULTS)) is None


# --- _rows / graph_meta / is_anchored ----------------------------------------
def test_graph_meta_reads_a_committed_key(graph_db: Path) -> None:
    assert c.graph_meta(graph_db, "root") == "app/src"


def test_graph_meta_of_an_unknown_key_is_empty(graph_db: Path) -> None:
    assert c.graph_meta(graph_db, "no_such_key") == ""


def test_is_anchored_is_true_only_for_anchored_paths(graph_db: Path) -> None:
    assert c.is_anchored(graph_db, "domain/FeedRanker.kt")
    assert not c.is_anchored(graph_db, "domain/Unmapped.kt")


@pytest.mark.parametrize("make", [
    lambda p: p.write_text("this is not a database", encoding="utf-8"),
    lambda p: p.write_bytes(b"SQLite format 3\x00truncated"),
    lambda p: None,  # does not exist at all
])
def test_a_broken_graph_reads_as_no_rows_rather_than_raising(
    tmp_path: Path, make: object
) -> None:
    db = tmp_path / "broken.db"
    make(db)  # type: ignore[operator]
    assert c.graph_meta(db, "root") == ""
    assert c.is_anchored(db, "anything") is False


def test_rows_does_not_leave_the_connection_open(graph_db: Path) -> None:
    """The hook runs per edit; a leaked handle per edit would be a real leak."""
    for _ in range(50):
        c.graph_meta(graph_db, "root")
    # A leaked read handle on Windows blocks this; on POSIX it would not.
    conn = sqlite3.connect(graph_db)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()


# --- is_source_file ----------------------------------------------------------
def test_a_file_outside_the_project_is_not_source(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.kt"
    assert not c.is_source_file(outside, tmp_path, dict(c.DEFAULTS))


@pytest.mark.parametrize("rel", [
    "node_modules/dep/index.js",
    ".git/COMMIT_EDITMSG",
    "build/generated/X.kt",
    "Pods/Thing.swift",            # case-folded: the set is not all-lowercase
    "DerivedData/Y.swift",
])
def test_ignored_directories_are_not_source(tmp_path: Path, rel: str) -> None:
    assert not c.is_source_file(tmp_path / rel, tmp_path, dict(c.DEFAULTS))


@pytest.mark.parametrize("name", [
    "NOTES.md", "data.json", "uv.lock", "config.yaml", "setup.cfg",
    ".gitignore", ".gitattributes", "rules.pro",
])
def test_docs_and_config_are_not_source(tmp_path: Path, name: str) -> None:
    assert not c.is_source_file(tmp_path / name, tmp_path, dict(c.DEFAULTS))


def test_a_dotfile_extension_is_matched_by_name_not_suffix(tmp_path: Path) -> None:
    # Path(".gitignore").suffix is "", so a naive suffix check would call it
    # source. The pre-push gate has the same carve-out.
    assert not c.is_source_file(tmp_path / ".gitignore", tmp_path, dict(c.DEFAULTS))


def test_code_under_the_root_is_source(tmp_path: Path) -> None:
    cfg = {**c.DEFAULTS, "root": "app/src"}
    assert c.is_source_file(tmp_path / "app" / "src" / "A.kt", tmp_path, cfg)


def test_code_outside_the_root_is_not_source(tmp_path: Path) -> None:
    cfg = {**c.DEFAULTS, "root": "app/src"}
    assert not c.is_source_file(tmp_path / "other" / "A.kt", tmp_path, cfg)


def test_an_empty_root_means_the_whole_project(tmp_path: Path) -> None:
    assert c.is_source_file(tmp_path / "anywhere" / "A.kt", tmp_path, dict(c.DEFAULTS))


def test_is_source_file_without_covers_prunes_ignored_dirs(tmp_path: Path) -> None:
    assert not c.is_source_file(
        tmp_path / ".githooks" / "pre-push", tmp_path, dict(c.DEFAULTS)
    )


def test_is_source_file_lets_covers_outrank_the_prune_and_the_extension_list(
    tmp_path: Path,
) -> None:
    cfg = {**c.DEFAULTS, "covers": [".githooks/*", "app/src/**/*.kt", "docs/*.md"]}
    assert c.is_source_file(tmp_path / ".githooks" / "pre-push", tmp_path, cfg)
    assert c.is_source_file(
        tmp_path / "app" / "src" / "main" / "build" / "Config.kt", tmp_path, cfg
    )
    assert c.is_source_file(tmp_path / "docs" / "a.md", tmp_path, cfg)
    assert not c.is_source_file(tmp_path / "app" / "build" / "Skip.kt", tmp_path, cfg)


def test_is_source_file_honours_exempt_and_root_with_covers(tmp_path: Path) -> None:
    cfg = {
        **c.DEFAULTS, "root": "app", "covers": ["src/**/*.kt"], "exempt": ["src/gen/*"],
    }
    assert c.is_source_file(tmp_path / "app" / "src" / "build" / "A.kt", tmp_path, cfg)
    assert not c.is_source_file(tmp_path / "app" / "src" / "gen" / "A.kt", tmp_path, cfg)
    assert not c.is_source_file(tmp_path / "other" / "src" / "A.kt", tmp_path, cfg)


# --- hooks.json --------------------------------------------------------------
def _handlers(tool: str, script: str = "kg_search_gate.py") -> list[dict[str, object]]:
    import json

    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    return [
        h for group in config["hooks"]["PreToolUse"] if group["matcher"] == tool
        for h in group["hooks"] if f"/{script.removesuffix('.py')}.cmd" in str(h["command"])
    ]


def _if_words(tool: str) -> set[str]:
    rules = [str(h["if"]) for h in _handlers(tool)]
    assert all(r.startswith(f"{tool}(") and r.endswith(" *)") for r in rules), rules
    return {r[len(tool) + 1:-3].lower() for r in rules}


def test_the_shell_if_rules_cover_every_search_word() -> None:
    """The gate starts only for commands an `if` rule names, so a search word the
    parser knows but no rule lists would never reach it."""
    import kg_search_gate as gate

    cmdlets = {"select-string", "sls", "get-childitem", "gci"}
    assert _if_words("PowerShell") == gate._SEARCH_WORDS
    assert _if_words("Bash") == (gate._SEARCH_WORDS - cmdlets) | {"sudo"}


def test_every_shell_handler_carries_an_if_rule() -> None:
    for tool in ("Bash", "PowerShell"):
        assert all("if" in h for h in _handlers(tool)), tool


def test_the_push_gate_starts_only_for_git_commands() -> None:
    handlers = _handlers("Bash", "kg_push_gate.py")
    assert [h["if"] for h in handlers] == ["Bash(git *)"]



# --- the interpreter launcher ------------------------------------------------
ROOT = HOOKS.parent
RUN = ROOT / "mcp" / "launch" / "run"
LAUNCH_PREFIX = '. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/'
# PowerShell reports only success or failure for `-Command` unless the command exits with the
# code itself. sh never reaches this part: the sourced launcher ends in `exec` or `exit`.
LAUNCH_TAIL = '.cmd"; exit $LASTEXITCODE'


def _launch_commands() -> list[tuple[str, str]]:
    """Every hook command string the plugin hands a shell."""
    import json

    return [
        ("hooks.json", str(h["command"]))
        for groups in json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"].values()
        for group in groups for h in group["hooks"]
    ]


def test_every_command_sources_one_launcher_that_runs_in_sh_and_powershell() -> None:
    """The one command string is read by sh on macOS, Linux and Git Bash, and by PowerShell on
    Windows without Git Bash. `. "<file>"` sources the file in sh and runs it in PowerShell; the
    placeholder is quoted, which both shells need for a path with a space. No `&&`, `||` or `$PY`:
    PowerShell 5.1 has no `&&`, and the interpreter is chosen inside the launcher. The tail hands
    the script's exit code to PowerShell."""
    commands = _launch_commands()
    assert len(commands) == 31
    for source, command in commands:
        assert command.startswith(LAUNCH_PREFIX) and command.endswith(LAUNCH_TAIL), (source, command)
        name = command[len(LAUNCH_PREFIX):-len(LAUNCH_TAIL)] + ".cmd"
        assert "/" not in name and (RUN / name).is_file(), (source, command)
        assert not any(t in command for t in ("&&", "||", "$PY")) and command.count(";") == 1, command


def test_the_launchers_are_one_template_with_the_script_swapped() -> None:
    template = (RUN / "kg_search_gate.cmd").read_bytes().decode("utf-8")
    scripts = {
        "kg_search_gate": "hooks", "kg_push_gate": "hooks", "kg_post_edit_check": "hooks",
        "kg_session_end": "hooks", "kg_server_start": "hooks", "kg_session_start": "hooks",
        "kg_headers": "mcp/launch",
    }
    assert sorted(p.stem for p in RUN.glob("*.cmd")) == sorted(scripts)
    back = chr(92)
    for stem, folder in scripts.items():
        text = (RUN / f"{stem}.cmd").read_bytes().decode("utf-8")
        assert chr(13) not in text, f"{stem}: sh cannot read a CR"
        assert (ROOT / folder / f"{stem}.py").is_file()
        want = template.replace("hooks/kg_search_gate.py", f"{folder}/{stem}.py")
        want = want.replace(f"hooks{back}kg_search_gate.py", f"{folder}/{stem}.py".replace("/", back))
        assert text == want, stem
    assert template.startswith(':<<"::KG_CMD"' + chr(10))
    code = [ln.lower() for ln in template.splitlines() if not ln.startswith("rem ")]
    assert not any("goto" in ln or "call :" in ln for ln in code)


def test_the_launchers_are_stored_with_lf_endings() -> None:
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert attrs.index("*.cmd text eol=crlf") < attrs.index("mcp/launch/run/*.cmd text eol=lf")


def _plugin_with(tmp_path: Path, stem: str, stubs: dict[str, str] | None = None,
                 folder: str = "plug in"):
    """A copy of the plugin's launcher in a folder whose name has a space, a hook script that
    records each run and prints the payload it read, and optional stub interpreters first on PATH."""
    import os
    import shutil

    root = tmp_path / folder
    (root / "mcp" / "launch" / "run").mkdir(parents=True)
    (root / "hooks").mkdir()
    shutil.copy(RUN / f"{stem}.cmd", root / "mcp" / "launch" / "run" / f"{stem}.cmd")
    runs = tmp_path / "runs"
    script = (
        "import sys\n"
        f"open({str(runs)!r}, 'a').write('x')\n"
        "print('got:' + sys.stdin.read().strip())\n"
        "sys.exit(3)\n"
    )
    (root / "hooks" / f"{stem}.py").write_bytes(script.encode())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (stubs or {}).items():
        (bin_dir / name).write_bytes(body.encode())
        (bin_dir / name).chmod(0o755)
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": root.as_posix(),
           "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}"}
    return runs, env


def _interpreter_stubs() -> dict[str, str]:
    py = Path(sys.executable).as_posix()
    # `py -3` drops its first word; the Microsoft Store python3 stub exits 9 when run.
    return {
        "py": f'#!/bin/sh\n[ "$1" = -3 ] && shift\nexec "{py}" "$@"\n',
        "python3": "#!/bin/sh\necho 'Python was not found' >&2\nexit 9\n",
        "python": f'#!/bin/sh\nexec "{py}" "$@"\n',
    }


@pytest.mark.parametrize("shell", ["sh", "bash", "dash"])
def test_a_failing_hook_script_runs_once_and_reads_the_payload_in_sh(tmp_path: Path, shell: str) -> None:
    """The launcher picks one interpreter and runs the script once: a script that exits non-zero
    must not be retried under the next interpreter, and a python3 that does not start is skipped."""
    import shutil
    import subprocess

    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"no {shell}")
    runs, env = _plugin_with(tmp_path, "kg_session_end", _interpreter_stubs())
    command = LAUNCH_PREFIX + "kg_session_end" + LAUNCH_TAIL
    done = subprocess.run([exe, "-c", command], input=b'{"k": 1}', env=env, capture_output=True,
                          timeout=30, check=False)
    assert done.returncode == 3, done.stderr
    assert done.stdout.decode().strip() == 'got:{"k": 1}'
    assert runs.read_text(encoding="utf-8") == "x"


def test_the_launcher_says_so_when_no_interpreter_starts_in_sh(tmp_path: Path) -> None:
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX shell")
    fail = "#!/bin/sh\nexit 9\n"
    runs, env = _plugin_with(tmp_path, "kg_session_end", {"py": fail, "python3": fail, "python": fail})
    done = subprocess.run([sh, "-c", LAUNCH_PREFIX + "kg_session_end" + LAUNCH_TAIL], env=env, capture_output=True,
                          timeout=30, check=False)
    assert done.returncode == 127 and b"no working Python" in done.stderr
    assert not runs.exists()


POWERSHELLS = ["powershell", "pwsh"]


def _powershell(shell: str, command: str, env: dict[str, str], payload: bytes = b""):
    """Run `command` the way Claude Code does on Windows without Git Bash."""
    import shutil
    import subprocess

    exe = shutil.which(shell)
    if sys.platform != "win32" or exe is None:
        pytest.skip(f"{shell} on Windows only")
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command], input=payload,
                          env=env, capture_output=True, timeout=60, check=False)


def _powershell_command(stem: str = "kg_session_end") -> str:
    """Claude Code rewrites the placeholder to `${env:CLAUDE_PLUGIN_ROOT}` for PowerShell."""
    return LAUNCH_PREFIX.replace("${CLAUDE_PLUGIN_ROOT}", "${env:CLAUDE_PLUGIN_ROOT}") + stem + LAUNCH_TAIL


@pytest.mark.parametrize("shell", POWERSHELLS)
@pytest.mark.parametrize("folder", ["plug in", "plug in (x86) & co", "plugin"])
def test_the_hook_command_runs_once_in_powershell_and_returns_the_exit_code(
    tmp_path: Path, shell: str, folder: str
) -> None:
    """PowerShell is the shell on Windows without Git Bash. The script reads the payload from
    stdin, runs once, and its exit code reaches the caller, as it does through sh."""
    runs, env = _plugin_with(tmp_path, "kg_session_end", folder=folder)
    done = _powershell(shell, _powershell_command(), env, b'{"k": 2}')
    assert done.stdout.decode().strip() == 'got:{"k": 2}', done.stderr
    assert done.returncode == 3
    assert runs.read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize("shell", POWERSHELLS)
def test_the_launcher_finds_the_script_in_powershell_without_the_variable(
    tmp_path: Path, shell: str
) -> None:
    """The batch half locates the script from its own folder, so a caller that expands the
    placeholder in the command text but exports no variable still works."""
    _, env = _plugin_with(tmp_path, "kg_session_end")
    root = env.pop("CLAUDE_PLUGIN_ROOT")
    command = _powershell_command().replace("${env:CLAUDE_PLUGIN_ROOT}", root)
    done = _powershell(shell, command, env, b'{"k": 3}')
    assert done.stdout.decode().strip() == 'got:{"k": 3}', done.stderr
    assert done.returncode == 3


@pytest.mark.parametrize("shell", POWERSHELLS)
def test_the_launcher_says_so_when_no_interpreter_starts_in_powershell(
    tmp_path: Path, shell: str
) -> None:
    import os
    import shutil

    exe = shutil.which(shell)
    runs, env = _plugin_with(tmp_path, "kg_session_end")
    # cmd.exe stays reachable; py, python3 and python do not.
    env["PATH"] = os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32")
    done = _powershell(exe or shell, _powershell_command(), env)
    assert done.returncode == 127 and b"no working Python" in done.stderr
    assert not runs.exists()


@pytest.mark.parametrize("shell", ["sh", "bash", "dash"])
def test_the_launcher_finds_the_script_in_sh_without_the_variable(tmp_path: Path, shell: str) -> None:
    """Without the variable, bash finds the plugin from the launcher's own path; a plain sh uses
    the working folder, which is the plugin folder when Claude Code runs the headers helper."""
    import shutil
    import subprocess

    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"no {shell}")
    _, env = _plugin_with(tmp_path, "kg_session_end", _interpreter_stubs())
    root = env.pop("CLAUDE_PLUGIN_ROOT")
    command = (LAUNCH_PREFIX + "kg_session_end" + LAUNCH_TAIL).replace("${CLAUDE_PLUGIN_ROOT}", root)
    done = subprocess.run([exe, "-c", command], input=b'{"k": 4}', env=env, cwd=root,
                          capture_output=True, timeout=30, check=False)
    assert done.returncode == 3, done.stderr
    assert done.stdout.decode().strip() == 'got:{"k": 4}'


# --- the headers helper -------------------------------------------------------
HEADERS_LAUNCHER = '"${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/kg_headers.cmd"'
HELPER_SHELLS = ["cmd", "powershell", "pwsh", "sh", "bash", "dash"]


def _headers_helper() -> str:
    import json

    mcp = json.loads((ROOT / "mcp" / "claude.mcp.json").read_text(encoding="utf-8"))
    return str(mcp["mcpServers"]["codebase-kg"]["headersHelper"])


def test_the_headers_helper_runs_the_launcher_in_cmd_and_sources_it_in_sh() -> None:
    """Claude Code starts the headers helper with Node's `shell: true`: `cmd.exe` on Windows and
    `/bin/sh` elsewhere, never Git Bash. `cmd.exe` cannot run `. "<file>"`. It reads
    `call="<file>"; ...` as `call "<file>"`, because `=` and `;` separate words there, and the
    launcher ignores the words it is passed. `sh` reads it as a variable assignment, then sources
    the launcher. PowerShell reports the first statement as an unknown command and runs the second."""
    assert _headers_helper() == f"call={HEADERS_LAUNCHER}; . {HEADERS_LAUNCHER}; exit $LASTEXITCODE"


def _run_helper(shell: str, root: Path, env: dict[str, str]):
    """Run the configured headers helper with the placeholder already replaced, as Claude Code does."""
    import os
    import shutil
    import subprocess

    windows_only = shell in ("cmd", "powershell", "pwsh")
    if windows_only and sys.platform != "win32":
        pytest.skip(f"{shell} on Windows only")
    if shell == "cmd":
        # Node's `shell: true` on Windows: ComSpec, `/d /s /c`, the command quoted once and
        # passed verbatim, which subprocess does with a string argument.
        comspec = os.environ.get("ComSpec", "cmd.exe")
        command = _headers_helper().replace("${CLAUDE_PLUGIN_ROOT}", str(root))
        args: object = f'"{comspec}" /d /s /c "{command}"'
    else:
        exe = shutil.which(shell)
        if exe is None:
            pytest.skip(f"no {shell}")
        command = _headers_helper().replace(
            "${CLAUDE_PLUGIN_ROOT}", str(root) if windows_only else root.as_posix())
        args = [exe, "-NoProfile", "-NonInteractive", "-Command", command] if windows_only else [exe, "-c", command]
    return subprocess.run(args, env=env, cwd=root, capture_output=True, timeout=60, check=False)


def _helper_plugin(tmp_path: Path, folder: str) -> tuple[Path, Path, dict[str, str]]:
    """The headers launcher in a plugin folder named `folder`, a stand-in `kg_headers.py` that
    records each run and prints one JSON object, and no `CLAUDE_PLUGIN_ROOT` in the environment."""
    import os
    import shutil

    root = tmp_path / folder
    (root / "mcp" / "launch" / "run").mkdir(parents=True)
    shutil.copy(RUN / "kg_headers.cmd", root / "mcp" / "launch" / "run" / "kg_headers.cmd")
    runs = tmp_path / "runs"
    (root / "mcp" / "launch" / "kg_headers.py").write_bytes((
        "import json\n"
        f"open({str(runs)!r}, 'a').write('x')\n"
        "print(json.dumps({'X-Codebase-KG-Client': 'c'}))\n"
    ).encode())
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    return root, runs, env


@pytest.mark.parametrize("shell", HELPER_SHELLS)
@pytest.mark.parametrize("folder", ["plugin", "plug in (x86) & co"])
def test_the_headers_helper_prints_one_json_object_once_in_each_shell(
    tmp_path: Path, shell: str, folder: str
) -> None:
    import json

    root, runs, env = _helper_plugin(tmp_path, folder)
    done = _run_helper(shell, root, env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"X-Codebase-KG-Client": "c"}
    assert runs.read_text(encoding="utf-8") == "x"
    if shell not in ("powershell", "pwsh"):
        assert done.stderr == b""


def test_the_headers_helper_says_so_in_cmd_when_no_interpreter_starts(tmp_path: Path) -> None:
    import os

    root, runs, env = _helper_plugin(tmp_path, "plug in")
    env["PATH"] = os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32")
    done = _run_helper("cmd", root, env)
    assert done.returncode == 127 and b"no working Python" in done.stderr
    assert done.stdout == b""
    assert not runs.exists()


@pytest.mark.parametrize("shell", ["cmd", "sh"])
def test_the_real_headers_helper_prints_its_headers_through_the_platform_shell(shell: str) -> None:
    """The plugin's own `kg_headers.py`, through the shell Claude Code uses on this platform. A
    program that is not codebase-kg answers on the port, so the helper sends no token and does not
    wait for a server."""
    import http.server
    import json
    import os
    import threading

    if (shell == "cmd") != (sys.platform == "win32"):
        pytest.skip("Claude Code uses cmd.exe on Windows and sh elsewhere")

    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    other = http.server.HTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "CODEBASE_KG_PATH")}
        env["CLAUDE_CODE_MCP_SERVER_URL"] = f"http://127.0.0.1:{other.server_address[1]}/mcp"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = _run_helper(shell, ROOT, env)
    finally:
        other.shutdown()
        other.server_close()
    assert done.returncode == 0, done.stderr
    assert len(done.stdout.decode().strip().splitlines()) == 1
    headers = json.loads(done.stdout)
    assert set(headers) == {"X-Codebase-KG-Client"}, headers


def test_the_async_credit_hook_says_why_it_is_async() -> None:
    import json

    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    (group,) = [g for g in config["hooks"]["PreToolUse"] if g["matcher"].startswith("mcp__")]
    assert all(h.get("async") is True for h in group["hooks"])
    assert "`async: true` on purpose" in config["description"]


# --- gate state is replaced, not rewritten in place -----------------------------
def test_write_state_replaces_the_file_through_a_temp_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os

    path = tmp_path / "state" / "s.json"
    assert c.write_state(path, {"credit": 3}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"credit": 3}

    seen: list[tuple[str, str]] = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append((str(a), str(b))), real(a, b))[1])
    assert c.write_state(path, {"credit": 2}) is True
    (src, dst), = seen
    assert dst == str(path) and src != dst and Path(src).parent == path.parent
    assert [p.name for p in path.parent.iterdir()] == ["s.json"]


def test_a_failed_replace_leaves_the_old_state_whole_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os

    path = tmp_path / "s.json"
    c.write_state(path, {"credit": 3})

    def refuse(src: object, dst: object) -> None:
        raise OSError("refused")

    monkeypatch.setattr(os, "replace", refuse)
    assert c.write_state(path, {"credit": 0}) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"credit": 3}
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_session_start_leaves_out_compact_on_purpose() -> None:
    """A compact does not stop the server or drop the session registration, so running the
    SessionStart hooks again would repeat work and could repeat the notice (hooks/README.md)."""
    import json

    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert {g["matcher"] for g in config["hooks"]["SessionStart"]} == {"startup|resume|clear"}
    assert "`compact` is left out on purpose" in (HOOKS / "README.md").read_text(encoding="utf-8")

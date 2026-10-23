"""Config + path helpers for the codebase-kg advisory hook. Stdlib only.

Reads optional per-repo config from `.claude/codebase-kg.local.md` (YAML-ish
frontmatter). Everything here is best-effort: callers must treat failures as
"no nudge", never as an error (an advisory hook must never break an edit).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

DEFAULTS: dict[str, object] = {
    "root": "",  # empty => the whole project
    # The graph always lives here; a .local.md may override via `graph_path`
    # (`kg_path` is still honored for checkouts predating the rename).
    "graph_path": "knowledge/code_graph.db",
    "post_edit_nudge": True,  # master off-switch for the hook
    "nudge_every": 5,  # nudge once per this many source edits since the graph was touched
    # The PreToolUse search gate: block | warn | off. See kg_search_gate.py.
    "search_gate": "block",
    "gate_shell_search": True,  # also gate grep/rg/find -name run through a shell
    # Unlocated searches one codebase-kg query clears. At 0 the repeat-to-override
    # escape hatch still applies, so no setting here can strand an agent.
    "gate_credit": 3,
    # Generic doc/config exclusions (NOT language detection) — these edits don't
    # imply graph drift. Kept identical to the pre-push gate's EXCLUDE_EXT; the
    # two are asserted equal by test_hook_parity.py, because the copies had
    # silently drifted when only a comment held them together.
    "exclude_ext": [
        ".md", ".markdown", ".txt", ".json", ".lock", ".yaml", ".yml", ".toml",
        ".cfg", ".ini", ".gitignore", ".gitattributes", ".pro",
    ],
}

# Directories whose edits never imply graph drift. Identical to the pre-push
# gate's IGNORE_DIRS — see the note above.
IGNORE_DIRS = {
    ".git", ".github", ".githooks", ".claude", "node_modules", "build", "dist",
    "out", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode",
    "target", "Pods", "DerivedData", ".next", "vendor",
}
# Precomputed: the set is not all-lowercase (Pods, DerivedData), so the
# comparison must fold case — and rebuilding it per call was pure waste.
_IGNORE_LOWER = {d.lower() for d in IGNORE_DIRS}


def project_dir(cwd: str | None) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd or ".").resolve()


def norm_path(p: Path) -> str:
    return os.path.normcase(str(p))


def as_int(value: object, default: int, minimum: int | None = None) -> int:
    """`value` as an int, or `default` when it does not parse or is below `minimum`.

    Settings are user-typed and state comes off disk, so both reach `int()` as
    arbitrary objects. A raise would silence the hook for that repo with no error.
    """
    try:
        out = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return default if minimum is not None and out < minimum else out


def norm_root(root: str) -> str:
    """`root` as a comparable prefix: posix separators, no slashes, `.` folded to ``.

    `.` and `` both mean the whole repo, but a literal `.` is truthy while being
    a prefix of no repo-relative path -- so every `startswith` against it fails
    and the graph matches nothing. It hides because the package JOINS paths
    (`repo / "."` is `repo`) while this file COMPARES them.
    """
    out = str(root).strip().replace("\\", "/").strip("/")
    return "" if out == "." else out


def skip_kg(env: Mapping[str, str] | None = None) -> bool:
    """True when `SKIP_KG` is set to anything but whitespace, as the git hooks read it."""
    return bool(((env if env is not None else os.environ).get("SKIP_KG") or "").strip())


def state_dir(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


def state_path(name: str, key: str) -> Path:
    return state_dir(name) / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.json"


def read_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, state: dict[str, object]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        return False
    return True


def emit_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def run_hook(run: Callable[[dict[str, object]], None]) -> None:
    """Run `run` on the JSON object from stdin. Any error returns silently: a hook
    that errors must never break the tool call or strand the agent."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            run(data)
    except Exception:  # noqa: BLE001
        return


def _coerce(value: str) -> object:
    v = value.strip()
    low = v.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if v.isdigit():
        return int(v)
    if v.startswith("[") and v.endswith("]"):
        return [s.strip().strip("'\"") for s in v[1:-1].split(",") if s.strip()]
    return v.strip("'\"")


def _parse_frontmatter(text: str) -> dict[str, object]:
    """Same rules as the pre-push gate and the MCP server: strip an inline
    `# comment` tail and ignore `<placeholder>` values.

    Those two rules are not cosmetic. Without the comment strip, a perfectly
    ordinary `root: app/src  # java only` yields a path that matches nothing, and
    this hook goes silent for the whole repo with no error anywhere.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, object] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = re.sub(r"\s+#.*$", "", val).strip()
        if key and val and not val.startswith("<"):
            out[key] = _coerce(val)
    return out


def load_config(proj: Path) -> dict[str, object]:
    cfg = dict(DEFAULTS)
    local = proj / ".claude" / "codebase-kg.local.md"
    try:
        if local.is_file():
            parsed = _parse_frontmatter(local.read_text(encoding="utf-8-sig"))
            if "kg_path" in parsed and "graph_path" not in parsed:
                parsed["graph_path"] = parsed["kg_path"]
            cfg.update(parsed)
    except (OSError, ValueError):
        pass
    return cfg


def as_local_path(raw: str) -> Path:
    """`/c/Users/...` is how Git Bash writes `C:/Users/...`."""
    m = re.match(r"^/([A-Za-z])/(.*)$", raw)
    if sys.platform == "win32" and m:
        return Path(f"{m.group(1)}:/{m.group(2)}")
    return Path(raw)


_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command: str) -> str:
    """The command with every heredoc body removed.

    A heredoc body is data, not commands — a commit message that discusses
    `grep` is not a search. An unterminated one swallows the rest, which errs
    toward not gating.
    """
    lines = command.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        kept.append(lines[i])
        match = _HEREDOC.search(lines[i])
        i += 1
        if match:
            delim = match.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # the closing delimiter is not a command either
    return "\n".join(kept)


def find_graph(proj: Path, cfg: dict[str, object]) -> Path | None:
    """Resolve the graph from `graph_path` (default: knowledge/code_graph.db). No
    root fallback — if the file isn't there, the repo has no graph yet and the
    hook no-ops."""
    raw = cfg.get("graph_path") or cfg.get("kg_path") or "knowledge/code_graph.db"
    p = Path(str(raw))
    if not p.is_absolute():
        p = proj / p
    return p.resolve() if p.is_file() else None


def _rows(graph_file: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple]:
    """Query the graph read-only. Any failure is "no rows" — this is an advisory
    hook and must never turn a bad graph file into a broken edit."""
    try:
        conn = sqlite3.connect(f"{graph_file.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def graph_meta(graph_file: Path, key: str) -> str:
    """Read one key from the graph's `meta` table — the committed, shared config
    (so `root` etc. don't depend on a per-dev `.local.md`)."""
    rows = _rows(graph_file, "SELECT value FROM meta WHERE key = ?", (key,))
    return str(rows[0][0]) if rows else ""


def is_anchored(graph_file: Path, rel_path: str) -> bool:
    """Does any node anchor on this path? An indexed probe, not a full scan.

    This runs on every source edit, so it has to be a probe: pulling every
    anchor path into a set to answer one boolean cost ~83 ms at 100k anchors
    against ~0.5 ms here.
    """
    return bool(
        _rows(graph_file, "SELECT 1 FROM anchor WHERE path = ? LIMIT 1", (rel_path,))
    )


def is_source_file(path: Path, proj: Path, cfg: dict[str, object]) -> bool:
    """True when an edit to `path` plausibly affects the graph (a code unit under
    `root`, not a doc/config, not in an ignored dir). The caller checks whether
    the file *is* the graph."""
    try:
        rel = path.relative_to(proj)
    except ValueError:
        return False  # outside the project
    dir_parts = {p.lower() for p in rel.parts[:-1]}
    if dir_parts & _IGNORE_LOWER:
        return False
    root = str(cfg.get("root") or "")
    if root:
        try:
            path.relative_to((proj / root).resolve())
        except ValueError:
            return False
    excl = {e.lower() for e in cfg.get("exclude_ext", [])}  # type: ignore[union-attr]
    # Match the pre-push gate's suffix logic so dotfiles work: Path(".gitignore")
    # has no .suffix, but ".gitignore" itself is the extension to exclude.
    name = path.name.lower()
    suffix = ("." + name.rsplit(".", 1)[1]) if "." in name else ""
    return suffix not in excl

"""Config + path helpers for the codebase-kg advisory hook. Stdlib only.

Reads optional per-repo config from `.claude/codebase-kg.local.md` (YAML-ish
frontmatter). Everything here is best-effort: callers must treat failures as
"no nudge", never as an error (an advisory hook must never break an edit).
"""

from __future__ import annotations

import os
import re
import sqlite3
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
            cfg.update(_parse_frontmatter(local.read_text(encoding="utf-8")))
    except OSError:
        pass
    return cfg


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
    if suffix in excl:
        return False
    return True

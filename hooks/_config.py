"""Config + path helpers for the codebase-kg advisory hook. Stdlib only.

Reads optional per-repo config from `.claude/codebase-kg.local.md` (YAML-ish
frontmatter). Everything here is best-effort: callers must treat failures as
"no nudge", never as an error (an advisory hook must never break an edit).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULTS: dict[str, object] = {
    "root": "",  # empty => the whole project
    "kg_path": "knowledge/KNOWLEDGE_GRAPH.md",  # the KG always lives here; a .local.md may override
    "post_edit_nudge": True,  # master off-switch for the hook
    "nudge_every": 5,  # nudge once per this many source edits since the KG was last touched
    # Generic doc/config exclusions (NOT language detection) — these edits don't imply KG drift.
    "exclude_ext": [
        ".md", ".markdown", ".txt", ".json", ".lock", ".yaml", ".yml",
        ".toml", ".cfg", ".ini", ".gitignore", ".gitattributes",
    ],
}

# Directories whose edits never imply KG drift.
IGNORE_DIRS = {
    ".git", ".claude", ".github", "node_modules", "build", "dist", "out",
    ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode", "target",
}


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
        if key and val.strip():
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


def find_kg(proj: Path, cfg: dict[str, object]) -> Path | None:
    """Resolve the KG from `kg_path` (default: knowledge/KNOWLEDGE_GRAPH.md). No
    root fallback — if the file isn't there, the repo has no KG yet and the hook
    no-ops."""
    kg_path = str(cfg.get("kg_path") or "knowledge/KNOWLEDGE_GRAPH.md")
    p = Path(kg_path)
    if not p.is_absolute():
        p = proj / p
    return p.resolve() if p.is_file() else None


def kg_header_value(kg_file: Path, key: str) -> str:
    """Read one `key:` from the KG header block — the committed, shared config
    (so `root` etc. don't depend on a per-dev `.local.md`)."""
    try:
        head = "\n".join(kg_file.read_text(encoding="utf-8").splitlines()[:40])
    except OSError:
        return ""
    m = re.search(rf"(?mi)^\s*{re.escape(key)}:\s*(.+?)\s*$", head)
    return re.sub(r"\s+#.*$", "", m.group(1)).strip() if m else ""


def is_source_file(path: Path, proj: Path, cfg: dict[str, object], kg: Path) -> bool:
    """True when an edit to `path` plausibly affects the KG (a code unit under
    `root`, not a doc/config, not in an ignored dir, not the KG itself)."""
    try:
        rel = path.relative_to(proj)
    except ValueError:
        return False  # outside the project
    dir_parts = {p.lower() for p in rel.parts[:-1]}
    if dir_parts & {d.lower() for d in IGNORE_DIRS}:
        return False
    root = str(cfg.get("root") or "")
    if root:
        try:
            path.relative_to((proj / root).resolve())
        except ValueError:
            return False
    excl = {e.lower() for e in cfg.get("exclude_ext", [])}  # type: ignore[union-attr]
    if path.suffix.lower() in excl:
        return False
    return True

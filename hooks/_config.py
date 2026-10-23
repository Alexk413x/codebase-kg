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
from collections.abc import Callable, Collection, Iterable, Mapping
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


# --- declared coverage (verbatim copy of codebase_kg/coverage.py) -------------
# Copied rather than imported: the hooks run without the package on sys.path.
# tests/test_hook_parity.py asserts the copies stay identical.
def glob_to_regex(pattern: str) -> str:
    """One gitignore-flavoured glob as a regex source string.

    `**/` spans zero or more path segments, `*` and `?` never cross a `/`, and a
    trailing `/` means "everything under this directory". Hand-written rather
    than delegated to `fnmatch`, whose `*` happily matches `/` and would make
    `app/*.kt` match `app/a/b/c.kt`.
    """
    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return "(?!)"  # matches nothing
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def compile_patterns(patterns: list[str] | tuple[str, ...]) -> re.Pattern[str] | None:
    """All patterns as one alternation, or None when there are none.

    One compiled pattern rather than a list of them: this is matched against
    every file in the tree, and a single regex alternation is what keeps that a
    linear walk instead of len(patterns) walks.
    """
    parts = [glob_to_regex(p) for p in patterns if p and p.strip()]
    if not parts:
        return None
    return re.compile("^(?:" + "|".join(parts) + ")$")


def matches_any(path: str, compiled: re.Pattern[str] | None) -> bool:
    if compiled is None:
        return False
    return compiled.match(path.replace("\\", "/")) is not None


def dir_may_hold(
    rel_dir: str, patterns: Iterable[str], ignored: Collection[str]
) -> bool:
    """Whether a `covers` pattern could match a file beneath the directory `rel_dir`.

    A tree walk prunes conventionally-non-source directories (`build`,
    `node_modules`, `.githooks`, ...) so a large repo stays a cheap walk, but the
    graph's own declaration outranks that guess: a directory named in `ignored`
    survives the prune when some pattern can match beneath it.

    Pattern segments are matched against the directory's segments, with `**`
    spanning any number of them. A match carried only by a leading wildcard
    (`**/*.py`, `*/x/*.py`) never admits a directory named in `ignored`: a pattern
    that names no directory declares nothing about `node_modules` or `.venv`. Once
    a literal segment has matched, as in `app/src/**/*.kt`, the wildcards below it
    may span ignored names such as `app/src/main/kotlin/com/acme/build`.

    `ignored` holds lowercase directory names.
    """
    dir_segs = [s for s in rel_dir.replace("\\", "/").split("/") if s]
    for raw in patterns:
        pat = raw.strip().replace("\\", "/")
        if not pat or pat.startswith("#"):
            continue
        if pat.endswith("/"):
            pat += "**"
        if _may_match(dir_segs, pat.split("/"), False, ignored):
            return True
    return False


def _may_match(
    dir_segs: list[str], pat_segs: list[str], anchored: bool, ignored: Collection[str]
) -> bool:
    if not dir_segs:
        return bool(pat_segs)
    if not pat_segs:
        return False
    head, name = pat_segs[0], dir_segs[0]
    if head == "**":
        if len(pat_segs) == 1:
            return anchored or not any(s.lower() in ignored for s in dir_segs)
        if _may_match(dir_segs, pat_segs[1:], anchored, ignored):
            return True
        if not anchored and name.lower() in ignored:
            return False
        return _may_match(dir_segs[1:], pat_segs, anchored, ignored)
    if "**" in head:
        return anchored or not any(s.lower() in ignored for s in dir_segs)
    if re.fullmatch(glob_to_regex(head), name) is None:
        return False
    literal = _WILDCARDS.search(head) is None
    if not literal and not anchored and name.lower() in ignored:
        return False
    return _may_match(dir_segs[1:], pat_segs[1:], anchored or literal, ignored)


_WILDCARDS = re.compile(r"[*?]")


def parse_patterns(raw: str | None) -> list[str]:
    """A stored meta value back into a pattern list.

    Newline-separated is the storage form; commas are accepted too because that
    is what someone writes by hand the first time.
    """
    if not raw:
        return []
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out
# --- end verbatim copy --------------------------------------------------------


def is_source_file(path: Path, proj: Path, cfg: dict[str, object]) -> bool:
    """True when an edit to `path` plausibly affects the graph (a code unit under
    `root`, not a doc/config, not in an ignored dir). The caller checks whether
    the file *is* the graph.

    A `covers` declaration in `cfg` decides alone: the extension deny-list and
    the ignored directories apply only when none is declared.
    """
    try:
        rel = path.relative_to(proj)
    except ValueError:
        return False  # outside the project
    root = str(cfg.get("root") or "")
    key = rel.as_posix()
    if root:
        try:
            key = path.relative_to((proj / root).resolve()).as_posix()
        except ValueError:
            return False
    covers = declared_patterns(cfg, "covers")
    if covers:
        return matches_any(key, compile_patterns(covers)) and not matches_any(
            key, compile_patterns(declared_patterns(cfg, "exempt"))
        )
    if {p.lower() for p in rel.parts[:-1]} & _IGNORE_LOWER:
        return False
    excl = {e.lower() for e in cfg.get("exclude_ext", [])}  # type: ignore[union-attr]
    # Match the pre-push gate's suffix logic so dotfiles work: Path(".gitignore")
    # has no .suffix, but ".gitignore" itself is the extension to exclude.
    name = path.name.lower()
    suffix = ("." + name.rsplit(".", 1)[1]) if "." in name else ""
    return suffix not in excl


def declared_patterns(cfg: Mapping[str, object], key: str) -> list[str]:
    value = cfg.get(key)
    if isinstance(value, str):
        return parse_patterns(value)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def load_declared_coverage(graph_file: Path, cfg: dict[str, object]) -> None:
    """Add the graph's `covers` and `exempt` to `cfg` unless `cfg` already sets them."""
    for key in ("covers", "exempt"):
        if not cfg.get(key):
            cfg[key] = parse_patterns(graph_meta(graph_file, key))


def reaches_mapped_code(
    candidate: Path, proj: Path, root: str, covers: Iterable[str] = ()
) -> bool:
    try:
        rel = candidate.resolve().relative_to(proj)
    except (ValueError, OSError):
        return False  # outside the project entirely
    scope = rel.as_posix().strip("/")
    if scope == ".":
        scope = ""
    # Either the search sits inside root, or it is a parent directory that
    # still contains root. Both reach mapped code.
    if root and scope and not (
        (scope + "/").startswith(root + "/") or (root + "/").startswith(scope + "/")
    ):
        return False
    patterns = list(covers)
    if not patterns:
        return not {p.lower() for p in rel.parts} & _IGNORE_LOWER
    key = scope
    if root:
        key = scope[len(root) + 1 :] if (scope + "/").startswith(root + "/") else ""
    if not {p.lower() for p in key.split("/")} & _IGNORE_LOWER:
        return True
    if candidate.is_file():
        return matches_any(key, compile_patterns(patterns))
    return dir_may_hold(key, patterns, _IGNORE_LOWER)

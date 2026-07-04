#!/usr/bin/env python3
"""Pre-push KG freshness gate — vendored, stdlib-only, portable.

Blocks a push when tracked **source** files under the configured `root` changed
in the about-to-push commits but the repo's `KNOWLEDGE_GRAPH.md` was not updated
to match — either it isn't in the changeset, or its header `refreshed:` date
isn't today. Escape hatch: `git push --no-verify`.

The changeset comes from git's pre-push stdin (one `<local_ref> <local_sha>
<remote_ref> <remote_sha>` line per pushed ref), so it gates exactly what is
being pushed — any branch, new branches (diffed from the merge-base with the
remote default, or every not-yet-remote commit), skipping ref deletions. A
manual run without stdin falls back to `@{u}...HEAD` / `origin/<default>...HEAD`
and fails loudly when no such base exists.

This is the **deterministic** half of the gate only. The semantic work — actually
updating the changed nodes, bumping their per-node `updated` dates, reconciling
`parity`/`counterpart` against the peer KG, and running `kg_validate` — is the
agent's job (`/codebase-kg:refresh`). On purpose, this script has **no dependency**
on the codebase-kg MCP package, so it can be copied (vendored) straight into any
repo's hooks and runs anywhere Python 3 + git exist.

Config (optional) is read from `.claude/codebase-kg.local.md` in the repo root:
`root` (only source under here triggers), `kg_path` (default:
`knowledge/KNOWLEDGE_GRAPH.md`; no repo-root fallback).
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

# Generic doc/config exclusions — changing these does not imply KG drift.
EXCLUDE_EXT = {
    ".md", ".markdown", ".txt", ".json", ".lock", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".gitignore", ".gitattributes", ".pro",
}
IGNORE_DIRS = {
    ".git", ".github", ".githooks", ".claude", "node_modules", "build", "dist",
    "out", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode", "target",
}


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return ""


def _is_zero(sha: str) -> bool:
    """git uses an all-zeros sha for 'no ref' (new branch / deleted ref)."""
    return bool(sha) and set(sha) == {"0"}


def parse_push_refs(stdin_text: str) -> list[tuple[str, str, str, str]]:
    """Parse git's pre-push stdin: one
    `<local_ref> <local_sha> <remote_ref> <remote_sha>` line per ref pushed."""
    refs: list[tuple[str, str, str, str]] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            refs.append((parts[0], parts[1], parts[2], parts[3]))
    return refs


def push_range() -> str | None:
    """Fallback for a manual run (no stdin): prefer the upstream tracking
    branch, else origin's default branch. Three-dot so only our side counts.
    Returns None when no base exists to diff against."""
    if _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip():
        return "@{u}...HEAD"
    for base in ("origin/main", "origin/master"):
        if _git("rev-parse", "--verify", "--quiet", base).strip():
            return f"{base}...HEAD"
    return None


def changed_files(rng: str) -> list[str]:
    out = _git("diff", "--name-only", rng)
    return [f for f in out.splitlines() if f.strip()]


def _new_ref_range(local_sha: str) -> str | None:
    """Diff base for a branch that doesn't exist on the remote yet: the
    merge-base with the remote default branch, when one exists."""
    for base in ("origin/HEAD", "origin/main", "origin/master"):
        mb = _git("merge-base", base, local_sha).strip()
        if mb:
            return f"{mb}..{local_sha}"
    return None


def changed_files_for_ref(local_sha: str, remote_sha: str) -> list[str]:
    """Files changed in the commits this ref push would publish."""
    if not _is_zero(remote_sha):
        # Three-dot: diff from the merge-base, so remote-side commits (e.g. on
        # a diverged ref being force-pushed) don't count as our changes.
        return changed_files(f"{remote_sha}...{local_sha}")
    rng = _new_ref_range(local_sha)
    if rng is not None:
        return changed_files(rng)
    # No remote base at all (e.g. first push to an empty remote): every commit
    # not already on some remote-tracking ref is being published.
    files: list[str] = []
    for c in _git("rev-list", local_sha, "--not", "--remotes").split():
        out = _git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", c)
        files += [f for f in out.splitlines() if f.strip()]
    return files


def changed_files_for_push(refs: list[tuple[str, str, str, str]]) -> list[str]:
    """Union of changed files across every ref being pushed. Ref deletions
    (all-zeros local sha) publish no commits and are skipped."""
    files: list[str] = []
    for _local_ref, local_sha, _remote_ref, remote_sha in refs:
        if _is_zero(local_sha):
            continue  # deleting a remote ref — nothing pushed
        files += changed_files_for_ref(local_sha, remote_sha)
    seen: set[str] = set()
    return [f for f in files if not (f in seen or seen.add(f))]


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    cfg: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            v = re.sub(r"\s+#.*$", "", v).strip().strip("'\"")
            if k.strip() and v and not v.startswith("<"):
                cfg[k.strip().lower()] = v
    return cfg


def load_config(repo: Path) -> dict[str, str]:
    local = repo / ".claude" / "codebase-kg.local.md"
    try:
        if local.is_file():
            return _parse_frontmatter(local.read_text(encoding="utf-8"))
    except OSError:
        pass
    return {}


def find_kg_rel(repo: Path, cfg: dict[str, str]) -> str | None:
    """KG path relative to the repo root, as it appears in `git diff` output.
    Default: knowledge/KNOWLEDGE_GRAPH.md; an explicit kg_path in .local.md
    overrides it. No root fallback — a repo without that file has no KG and isn't
    gated."""
    kg_path = cfg.get("kg_path") or "knowledge/KNOWLEDGE_GRAPH.md"
    rel = kg_path.replace("\\", "/").removeprefix("./")
    return rel if (repo / rel).is_file() else None


def is_source(rel: str, root: str, kg_rel: str | None) -> bool:
    rel = rel.replace("\\", "/")
    if kg_rel and rel == kg_rel:
        return False
    if root and not (rel == root or rel.startswith(root.rstrip("/") + "/")):
        return False
    parts = rel.split("/")
    if set(p.lower() for p in parts[:-1]) & {d.lower() for d in IGNORE_DIRS}:
        return False
    suffix = ("." + rel.rsplit(".", 1)[1].lower()) if "." in parts[-1] else ""
    if suffix in EXCLUDE_EXT:
        return False
    return True


def kg_refreshed_today(kg_file: Path, today: str) -> bool:
    """True if the KG header `refreshed:` (or legacy `last refreshed`) is today."""
    try:
        text = kg_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    head = "\n".join(text.splitlines()[:40])
    return bool(
        re.search(rf"(?mi)^\s*refreshed:\s*{re.escape(today)}\b", head)
        or re.search(rf"(?i)last refreshed {re.escape(today)}\b", head)
    )


def kg_header_value(kg_file: Path, key: str) -> str:
    """Read one `key:` from the KG header block — the committed, shared config."""
    try:
        text = kg_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    head = "\n".join(text.splitlines()[:40])
    m = re.search(rf"(?mi)^\s*{re.escape(key)}:\s*(.+?)\s*$", head)
    return re.sub(r"\s+#.*$", "", m.group(1)).strip() if m else ""


def gate_decision(
    source_changed: bool, kg_in_changeset: bool, kg_fresh: bool
) -> tuple[bool, str]:
    """Pure decision. Returns (block, reason)."""
    if not source_changed:
        return (False, "no source changes — nothing to record")
    if not kg_in_changeset:
        return (True, "source changed but KNOWLEDGE_GRAPH.md was not updated")
    if not kg_fresh:
        return (True, "KNOWLEDGE_GRAPH.md header `refreshed:` is not today")
    return (False, "KG updated alongside source")


def _emit_block(reason: str, kg_rel: str, today: str, triggers: list[str]) -> None:
    msg = [
        "[codebase-kg] PUSH BLOCKED — " + reason + ".",
        "[codebase-kg]",
        f"[codebase-kg]   Source under the KG's root changed, but {kg_rel} is not in sync.",
        "[codebase-kg]   Run  /codebase-kg:refresh  (updates the changed nodes + their",
        f"[codebase-kg]   `updated` dates, reconciles parity vs the peer KG, sets refreshed: {today}),",
        "[codebase-kg]   then commit the KG and push again.",
        "[codebase-kg]",
        "[codebase-kg]   Override (advisory):  git push --no-verify",
        "[codebase-kg]",
        "[codebase-kg]   Source files that triggered this:",
    ]
    for t in triggers[:20]:
        msg.append(f"[codebase-kg]     {t}")
    sys.stderr.write("\n".join(msg) + "\n")


def main() -> int:
    repo = Path(_git("rev-parse", "--show-toplevel").strip() or ".").resolve()
    cfg = load_config(repo)
    kg_rel = find_kg_rel(repo, cfg)
    if kg_rel is None:
        return 0  # no KG in this repo → nothing to gate
    # `root` is the committed, shared config in the KG header; an optional per-dev
    # .claude/codebase-kg.local.md may override it. No committed config file needed.
    root = (cfg.get("root") or kg_header_value(repo / kg_rel, "root")).replace("\\", "/").strip("/")
    today = datetime.date.today().isoformat()

    # git feeds the pushed refs on stdin (`<local_ref> <local_sha> <remote_ref>
    # <remote_sha>` per ref) — that is the authoritative changeset. Only when
    # stdin is empty (manual invocation) fall back to guessing a range.
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    refs = parse_push_refs(stdin_text)
    if refs:
        changed = changed_files_for_push(refs)
    else:
        rng = push_range()
        if rng is None:
            sys.stderr.write(
                "[codebase-kg] pre-push gate: no push info on stdin and no upstream or\n"
                "[codebase-kg] origin default branch to diff against — cannot determine\n"
                "[codebase-kg] the push range. Run via `git push`, or set an upstream\n"
                "[codebase-kg] (git branch --set-upstream-to=<remote>/<branch>).\n"
            )
            return 1
        changed = changed_files(rng)
    triggers = [f for f in changed if is_source(f, root, kg_rel)]
    source_changed = bool(triggers)
    kg_in_changeset = kg_rel in [f.replace("\\", "/") for f in changed]
    kg_fresh = kg_refreshed_today(repo / kg_rel, today)

    block, reason = gate_decision(source_changed, kg_in_changeset, kg_fresh)
    if block:
        _emit_block(reason, kg_rel, today, triggers)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

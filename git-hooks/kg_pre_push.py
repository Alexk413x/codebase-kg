#!/usr/bin/env python3
"""Pre-push code-graph staleness check — vendored, stdlib-only, portable, advisory.

Reports when the commits you are about to push move the code away from what
`knowledge/code_graph.db` says about it:

  * a source file changed in this push that **no node anchors on** — new code
    nobody mapped;
  * a source file **deleted** in this push that the graph still anchors on — a
    pointer into code that is gone.

It **never blocks**. Exit status is always 0. That is a deliberate reversal: the
old version of this hook blocked a push when the graph's `refreshed:` header was
not today's date, which contradicted the plugin's own "advisory, never blocking"
principle and — worse — measured the wrong thing. A date says somebody edited the
file; it cannot say whether the *nodes* match the code. These checks compare the
graph against the actual changeset, so they are facts rather than a proxy.

The changeset comes from git's pre-push stdin (one `<local_ref> <local_sha>
<remote_ref> <remote_sha>` line per pushed ref), so it reflects exactly what is
being pushed — any branch, new branches (diffed from the merge-base with the
remote default, or every not-yet-remote commit), skipping ref deletions. A manual
run without stdin falls back to `@{u}...HEAD` / `origin/<default>...HEAD`.

This script has **no dependency** on the codebase-kg MCP package (sqlite3 is
stdlib), so it can be copied straight into any repo's hooks and runs anywhere
Python 3 + git exist.

Config (optional), from `.claude/codebase-kg.local.md` in the repo root:
`root` (only source under here counts) and `graph_path` (default
`knowledge/code_graph.db`; `kg_path` is still read for older checkouts).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_GRAPH = "knowledge/code_graph.db"

# Fallback only. When the graph declares `covers` (schema v3+) that declaration
# wins outright, because it is the repo's own statement of what should be mapped
# rather than this file's guess. These extensions are what a graph with no
# declaration falls back to — and a graph in that state cannot report a file
# type it has never covered, which is exactly why `covers` exists.
EXCLUDE_EXT = {
    ".md", ".markdown", ".txt", ".json", ".lock", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".gitignore", ".gitattributes", ".pro",
}
IGNORE_DIRS = {
    ".git", ".github", ".githooks", ".claude", "node_modules", "build", "dist",
    "out", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vscode",
    "target", "Pods", "DerivedData", ".next", "vendor",
}
_IGNORE_LOWER = {d.lower() for d in IGNORE_DIRS}


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
    branch, else origin's default branch. Three-dot so only our side counts."""
    if _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip():
        return "@{u}...HEAD"
    for base in ("origin/main", "origin/master"):
        if _git("rev-parse", "--verify", "--quiet", base).strip():
            return f"{base}...HEAD"
    return None


def changed_files(rng: str) -> list[tuple[str, str]]:
    """`(status, path)` pairs for a range. Status is git's letter: A/M/D/R…"""
    out = _git("diff", "--name-status", rng)
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        # A rename reports `R100\told\tnew` — the new path is what exists now,
        # and the old one is a deletion the graph may still be pointing at.
        if status == "R" and len(parts) >= 3:
            pairs.append(("D", parts[1]))
            pairs.append(("A", parts[2]))
        else:
            pairs.append((status, parts[-1]))
    return pairs


def _new_ref_range(local_sha: str) -> str | None:
    """Diff base for a branch that doesn't exist on the remote yet."""
    for base in ("origin/HEAD", "origin/main", "origin/master"):
        mb = _git("merge-base", base, local_sha).strip()
        if mb:
            return f"{mb}..{local_sha}"
    return None


def changed_files_for_ref(local_sha: str, remote_sha: str) -> list[tuple[str, str]]:
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
    pairs: list[tuple[str, str]] = []
    for c in _git("rev-list", local_sha, "--not", "--remotes").split():
        out = _git("diff-tree", "--no-commit-id", "--name-status", "-r", "--root", c)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                pairs.append((parts[0][:1], parts[-1]))
    return pairs


def changed_files_for_push(refs: list[tuple[str, str, str, str]]) -> list[tuple[str, str]]:
    """Union of changed files across every ref being pushed. Ref deletions
    (all-zeros local sha) publish no commits and are skipped."""
    pairs: list[tuple[str, str]] = []
    for _local_ref, local_sha, _remote_ref, remote_sha in refs:
        if _is_zero(local_sha):
            continue  # deleting a remote ref — nothing pushed
        pairs += changed_files_for_ref(local_sha, remote_sha)
    return list(dict.fromkeys(pairs))


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


def find_graph_rel(repo: Path, cfg: dict[str, str]) -> str | None:
    """Graph path relative to the repo root, as it appears in `git diff` output.
    `kg_path` is honored so a checkout predating the rename keeps working."""
    raw = cfg.get("graph_path") or cfg.get("kg_path") or DEFAULT_GRAPH
    rel = raw.replace("\\", "/").removeprefix("./")
    return rel if (repo / rel).is_file() else None


# --- declared coverage (verbatim copy of codebase_kg/coverage.py) -------------
# Copied rather than imported: this file is vendored into repos that have no
# plugin install. tests/test_hook_parity.py asserts the two stay identical.
def glob_to_regex(pattern: str) -> str:
    """One gitignore-flavoured glob as a regex source string."""
    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return "(?!)"
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


def compile_patterns(patterns):
    """All patterns as one alternation, or None when there are none."""
    parts = [glob_to_regex(p) for p in patterns if p and p.strip()]
    if not parts:
        return None
    return re.compile("^(?:" + "|".join(parts) + ")$")


def matches_any(path: str, compiled) -> bool:
    if compiled is None:
        return False
    return compiled.match(path.replace("\\", "/")) is not None


def parse_patterns(raw):
    """A stored meta value back into a pattern list."""
    if not raw:
        return []
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


# --- end verbatim copy --------------------------------------------------------


def is_source(rel: str, root: str, graph_rel: str | None, covers=None, exempt=None) -> bool:
    """Does a changed file count as source the graph should have mapped?

    With a `covers` declaration the answer comes from the repo's own statement
    of scope; without one it falls back to the extension deny-list, which cannot
    see a file type nobody has ever covered.
    """
    rel = rel.replace("\\", "/")
    if graph_rel and rel == graph_rel:
        return False
    if root and not (rel == root or rel.startswith(root.rstrip("/") + "/")):
        return False
    parts = rel.split("/")
    # The declaration is consulted *before* IGNORE_DIRS, not after. Those
    # directory names are a guess at what is never source; `covers` is the
    # repo's own statement of what is. A repo that declares `.githooks/*` was
    # being told its files did not count, by a deny-list that outranked the
    # declaration this function documents as winning outright.
    if covers is not None:
        key = _rel_to_root(rel, root)
        return matches_any(key, covers) and not matches_any(key, exempt)
    if {p.lower() for p in parts[:-1]} & _IGNORE_LOWER:
        return False
    suffix = ("." + rel.rsplit(".", 1)[1].lower()) if "." in parts[-1] else ""
    if suffix in EXCLUDE_EXT:
        return False
    return True


def read_graph(db: Path) -> tuple[str, set[str], list[str], list[str]] | None:
    """`(root, anchored_paths, covers, exempt)`, or None if it can't be read.

    Unreadable is not an error worth shouting about in a push hook — the check
    simply doesn't run.
    """
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        meta = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('root', 'covers', 'exempt')"
            )
        }
        paths = {r[0].replace("\\", "/") for r in conn.execute("SELECT DISTINCT path FROM anchor")}
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return (
        meta.get("root", ""),
        paths,
        parse_patterns(meta.get("covers")),
        parse_patterns(meta.get("exempt")),
    )


def _rel_to_root(rel: str, root: str) -> str:
    """A repo-relative path, re-expressed relative to the graph's `root`.

    Anchors are stored relative to `root` (SCHEMA.md §3) while git reports paths
    relative to the repo, so one of the two has to be translated before they can
    be compared.
    """
    rel = rel.replace("\\", "/")
    root = root.replace("\\", "/").strip("/")
    if root and rel.startswith(root + "/"):
        return rel[len(root) + 1 :]
    return rel


def analyze(
    changed: list[tuple[str, str]],
    root: str,
    graph_rel: str | None,
    anchored: set[str],
    covers: list[str] | None = None,
    exempt: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split the changeset into `(unmapped_new, anchored_but_deleted)`."""
    covers_re = compile_patterns(covers or [])
    exempt_re = compile_patterns(exempt or [])
    unmapped: list[str] = []
    deleted: list[str] = []
    for status, rel in changed:
        if not is_source(rel, root, graph_rel, covers_re, exempt_re):
            continue
        key = _rel_to_root(rel, root)
        if status == "D":
            if key in anchored:
                deleted.append(rel)
        elif status == "A" and key not in anchored:
            unmapped.append(rel)
    return unmapped, deleted


def _emit(unmapped: list[str], deleted: list[str], graph_rel: str) -> None:
    msg = [
        "[codebase-kg] Code-graph staleness check (advisory — your push is going through).",
        "[codebase-kg]",
    ]
    if deleted:
        msg.append(f"[codebase-kg]   {len(deleted)} deleted file(s) still anchored in {graph_rel}:")
        for f in deleted[:15]:
            msg.append(f"[codebase-kg]     - {f}")
        if len(deleted) > 15:
            msg.append(f"[codebase-kg]     … and {len(deleted) - 15} more")
        msg.append("[codebase-kg]")
    if unmapped:
        msg.append(f"[codebase-kg]   {len(unmapped)} new source file(s) that no node covers:")
        for f in unmapped[:15]:
            msg.append(f"[codebase-kg]     + {f}")
        if len(unmapped) > 15:
            msg.append(f"[codebase-kg]     … and {len(unmapped) - 15} more")
        msg.append("[codebase-kg]")
    msg += [
        "[codebase-kg]   Run  /codebase-kg:refresh  to bring the graph back in line.",
        "[codebase-kg]   Nothing is blocked; this is a heads-up.",
    ]
    sys.stderr.write("\n".join(msg) + "\n")


def main() -> int:
    repo = Path(_git("rev-parse", "--show-toplevel").strip() or ".").resolve()
    cfg = load_config(repo)
    graph_rel = find_graph_rel(repo, cfg)
    if graph_rel is None:
        return 0  # no graph in this repo → nothing to check
    loaded = read_graph(repo / graph_rel)
    if loaded is None:
        return 0  # unreadable store → stay silent rather than nag
    graph_root, anchored, covers, exempt = loaded

    # `root` is the committed, shared config in the graph itself; an optional
    # per-dev .claude/codebase-kg.local.md may override it.
    root = (cfg.get("root") or graph_root).replace("\\", "/").strip("/")

    # git feeds the pushed refs on stdin — that is the authoritative changeset.
    # Only when stdin is empty (manual invocation) fall back to guessing a range.
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    refs = parse_push_refs(stdin_text)
    if refs:
        changed = changed_files_for_push(refs)
    else:
        rng = push_range()
        if rng is None:
            return 0  # nothing to compare against — advisory checks stay quiet
        changed = changed_files(rng)

    unmapped, deleted = analyze(changed, root, graph_rel, anchored, covers, exempt)
    if unmapped or deleted:
        _emit(unmapped, deleted, graph_rel)
    return 0  # advisory, always


if __name__ == "__main__":
    sys.exit(main())

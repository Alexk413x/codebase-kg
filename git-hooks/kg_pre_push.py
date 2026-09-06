#!/usr/bin/env python3
"""Pre-push code-graph staleness check — vendored, stdlib-only, portable, advisory.

Reports when the commits you are about to push move the code away from what
`knowledge/code_graph.db` says about it:

  * a source file changed in this push that **no node anchors on** — code nobody
    mapped, whether it was added here or merely touched here;
  * a source file **deleted** in this push that the graph still anchors on — a
    pointer into code that is gone;
  * a **mapped** file whose contents no longer match the digest recorded when
    the graph was built (SCHEMA.md §6.3) — the anchor still resolves, so nothing
    else notices, but the description may no longer fit.

The third one is why the first two were not enough. A change set is mostly
status `M`, and a check that reads only `A` and `D` is silent through exactly
the drift that accumulates: on one repo it let a graph fall 48 commits behind,
of which two thirds were modifications it never mentioned.

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

import hashlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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


def blob_digests(specs: list[str]) -> dict[str, str]:
    """SHA-256 of each `<rev>:<path>` blob, skipping any git cannot resolve.

    One `git cat-file --batch` for the whole set rather than a process per file:
    this runs while someone is waiting on a commit or a push, and a change set
    of a few hundred files is ordinary.

    The digest must be computed the same way `writer.file_sha` computes it —
    SHA-256 over the raw bytes, no decoding — or every comparison reads as
    permanent drift.
    """
    if not specs:
        return {}
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            input="\n".join(specs).encode("utf-8") + b"\n",
            capture_output=True,
            check=False,
        )
    except OSError:
        return {}
    out, pos, result = proc.stdout, 0, {}
    for spec in specs:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        # `<spec> missing` for anything unresolvable — a path added in this
        # change has no blob at an older rev, which is not an error. No content
        # follows such a line, so `pos` is already correct.
        if len(header) < 3 or header[1] != "blob":
            continue
        try:
            size = int(header[2])
        except ValueError:
            break  # desynced from the stream — stop rather than misalign
        result[spec] = hashlib.sha256(out[pos : pos + size]).hexdigest()
        pos += size + 1  # git writes a newline after the content
    return result


def digests_for(paths: list[str], revs: list[str]) -> dict[str, str]:
    """SHA-256 per repo-relative path, read from the first `rev` that has it.

    Read out of git, never off disk. A push of a branch that is not checked out
    would otherwise be digested against whatever happens to be in the working
    tree and report drift on files the push does not touch. `""` as a rev means
    the index — `:path` is the staged blob, which is what a commit will contain.
    """
    out: dict[str, str] = {}
    for rev in revs:
        todo = [p for p in paths if p not in out]
        if not todo:
            break
        by_spec = {f"{rev}:{p}": p for p in todo}
        for spec, sha in blob_digests(list(by_spec)).items():
            out[by_spec[spec]] = sha
    return out


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


def push_tips(refs: list[tuple[str, str, str, str]]) -> list[str]:
    """The commits being published, in the order git listed their refs.

    These are the revisions whose blobs are the content of this push, so they
    are what a digest comparison has to read. Ordered rather than merged: a push
    of several refs is rare, and trying them in turn resolves each path against
    the first tip that has it instead of guessing.
    """
    return [local for _lr, local, _rr, _rs in refs if not _is_zero(local)]


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


class Graph(NamedTuple):
    """What the check needs out of the store, read once."""

    root: str
    anchored: set[str]
    covers: list[str]
    exempt: list[str]
    baselines: dict[str, str]  # root-relative path → sha256 at build time (§6.3)


def read_graph(db: Path) -> Graph | None:
    """The graph's root, anchors, coverage declaration and source baselines, or
    None if it can't be read.

    Unreadable is not an error worth shouting about in a push hook — the check
    simply doesn't run. A graph predating the `source` table reads as no
    baselines, which means no drift reported: absent evidence, never "unchanged".
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
        try:
            baselines = {
                r[0].replace("\\", "/"): r[1] for r in conn.execute("SELECT path, sha FROM source")
            }
        except sqlite3.DatabaseError:
            # Schema predates the table. The other two checks still work, so
            # losing the baselines must not lose the whole run.
            baselines = {}
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return Graph(
        meta.get("root", ""),
        paths,
        parse_patterns(meta.get("covers")),
        parse_patterns(meta.get("exempt")),
        baselines,
    )


def norm_root(root: str) -> str:
    """`root` as a comparable prefix: posix separators, no slashes, `.` folded to ``.

    `.` and `` both mean the whole repo, but a literal `.` is truthy while being
    a prefix of no repo-relative path -- so every `startswith` against it fails
    and the graph matches nothing. It hides because the package JOINS paths
    (`repo / "."` is `repo`) while this file COMPARES them.
    """
    out = str(root).strip().replace("\\", "/").strip("/")
    return "" if out == "." else out


def _rel_to_root(rel: str, root: str) -> str:
    """A repo-relative path, re-expressed relative to the graph's `root`.

    Anchors are stored relative to `root` (SCHEMA.md §3) while git reports paths
    relative to the repo, so one of the two has to be translated before they can
    be compared.
    """
    rel = rel.replace("\\", "/")
    root = norm_root(root)
    if root and rel.startswith(root + "/"):
        return rel[len(root) + 1 :]
    return rel


class Findings(NamedTuple):
    """The three ways a change set can move the code away from the graph."""

    unmapped: list[str]  # source in this change that no node anchors on
    deleted: list[str]  # source removed that the graph still anchors on
    drifted: list[str]  # mapped source whose bytes no longer match the baseline


def drift_candidates(
    changed: list[tuple[str, str]],
    root: str,
    graph_rel: str | None,
    anchored: set[str],
    covers: list[str] | None = None,
    exempt: list[str] | None = None,
) -> list[str]:
    """The paths worth digesting: in-scope source that a node actually anchors.

    Split out of `analyze` so the caller hashes only files that could possibly
    drift. Digesting the whole change set would read blobs for deletions and for
    files nobody mapped, which no comparison would ever use.
    """
    covers_re = compile_patterns(covers or [])
    exempt_re = compile_patterns(exempt or [])
    out: list[str] = []
    for status, rel in changed:
        if status == "D" or not is_source(rel, root, graph_rel, covers_re, exempt_re):
            continue
        if _rel_to_root(rel, root) in anchored:
            out.append(rel)
    return list(dict.fromkeys(out))


def analyze(
    changed: list[tuple[str, str]],
    root: str,
    graph_rel: str | None,
    anchored: set[str],
    covers: list[str] | None = None,
    exempt: list[str] | None = None,
    baselines: dict[str, str] | None = None,
    current: dict[str, str] | None = None,
) -> Findings:
    """Split the changeset into unmapped / deleted / drifted.

    `baselines` maps a root-relative path to the digest recorded when the graph
    was built; `current` maps a repo-relative path to its digest in the change
    set being checked. With neither, the drift bucket is empty — a missing
    baseline reads as "no baseline", never as "unchanged" (SCHEMA.md §6.3).
    """
    covers_re = compile_patterns(covers or [])
    exempt_re = compile_patterns(exempt or [])
    baselines = baselines or {}
    current = current or {}
    unmapped: list[str] = []
    deleted: list[str] = []
    drifted: list[str] = []
    for status, rel in changed:
        if not is_source(rel, root, graph_rel, covers_re, exempt_re):
            continue
        key = _rel_to_root(rel, root)
        if status == "D":
            if key in anchored:
                deleted.append(rel)
        elif key not in anchored:
            # Not keyed on `A`. A file nobody mapped is a gap whether it was
            # added in this change or merely touched by it — keying on `A` alone
            # meant a file that predates the graph stayed invisible forever.
            unmapped.append(rel)
        else:
            base, now = baselines.get(key), current.get(rel)
            if base and now and base != now:
                drifted.append(rel)
    # A push spanning several commits reports one path under more than one
    # status, so the same file reaches a bucket twice and the count doubles.
    return Findings(*(list(dict.fromkeys(b)) for b in (unmapped, deleted, drifted)))


def _block(msg: list[str], files: list[str], heading: str, marker: str) -> None:
    """One findings section, capped so a large change set stays readable."""
    msg.append(f"[codebase-kg]   {heading}")
    for f in files[:15]:
        msg.append(f"[codebase-kg]     {marker} {f}")
    if len(files) > 15:
        msg.append(f"[codebase-kg]     … and {len(files) - 15} more")
    msg.append("[codebase-kg]")


def _emit(findings: Findings, graph_rel: str, action: str = "push") -> None:
    """Report the findings on stderr.

    `action` names the change set being reported on. It is a parameter because
    this function is shared with the commit hook: hardcoding "push" made that
    hook announce a push that was not happening, and the fix at the time was a
    correcting line printed underneath — so every commit-time report contradicted
    its own header two lines later.
    """
    unmapped, deleted, drifted = findings
    scope = "staged changes" if action == "commit" else "commits being pushed"
    msg = [
        f"[codebase-kg] Code-graph staleness check on the {scope} "
        f"(advisory - your {action} is going through).",
        "[codebase-kg]",
    ]
    if deleted:
        _block(msg, deleted, f"{len(deleted)} deleted file(s) still anchored in {graph_rel}:", "-")
    if unmapped:
        _block(msg, unmapped, f"{len(unmapped)} source file(s) that no node covers:", "+")
    if drifted:
        _block(
            msg,
            drifted,
            f"{len(drifted)} mapped file(s) changed since {graph_rel} was built - "
            "the anchors still resolve, the descriptions may not:",
            "~",
        )
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
    graph = read_graph(repo / graph_rel)
    if graph is None:
        return 0  # unreadable store → stay silent rather than nag

    # `root` is the committed, shared config in the graph itself; an optional
    # per-dev .claude/codebase-kg.local.md may override it.
    root = norm_root(cfg.get("root") or graph.root)

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

    candidates = drift_candidates(
        changed, root, graph_rel, graph.anchored, graph.covers, graph.exempt
    )
    # The pushed tips, so the digest is of what is being published rather than
    # of whatever the working tree happens to hold. `HEAD` is the fallback for a
    # manual run, where the range came from the upstream branch anyway.
    current = digests_for(candidates, push_tips(refs) or ["HEAD"]) if candidates else {}

    findings = analyze(
        changed, root, graph_rel, graph.anchored, graph.covers, graph.exempt,
        graph.baselines, current,
    )
    if any(findings):
        _emit(findings, graph_rel)
    return 0  # advisory, always


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pre-push code-graph staleness check — vendored, stdlib-only, portable.

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

The first three findings are scoped to the change set and are advisory. That
scoping is right for per-commit noise and wrong for a standing gap: a file that
drifts and is never re-derived is reported once, in the commit that touched it,
and never again. One consumer repo carried 47 stale files for months with every
one of these checks passing. So this hook also asks the repo-wide question —
every anchored file against its baseline — and **blocks on any stale mapped
file**.

The gate covers every stale file, including the ones this push touches. A push
is publication: it records the code on the remote, and a graph that lags the
code at that point stays wrong for everyone who reads it. Refresh the graph
before you push, or acknowledge the drift explicitly. In a repo that is kept
current no file is stale and this hook is silent.

When the gate would block, the hook first tries to fix the cause: it runs
`claude -p "/codebase-kg:refresh"` headless in the repo root, with the stale
files listed in the prompt. The run reads the repo and writes only through the
graph's MCP write tools: no shell, no `Write`, `Edit` or CLI runner, and a
minimal environment. It does this only when all of these hold:

  * `claude` is on `PATH`;
  * `KG_AUTO_REFRESH` is not `0`, and `KG_REFRESHING` is not set (the recursion
    guard the hook sets for the run);
  * a pushed ref's local sha is `HEAD`, so the refreshed graph can be committed
    on top of what is being pushed;
  * the graph file has no uncommitted changes.

If the run exits 0, the graph file changed and no mapped file is stale against
`HEAD`, the hook commits the graph as "Refresh the code graph" and still exits
1. A pre-push hook cannot add a commit to the push in progress, so you run
`git push` again. Every other outcome — `claude` fails or times out (900 s), a
file stays stale, anything raises — prints the reason, leaves any graph change
uncommitted, and falls through to the normal block. A refresh costs one headless
model run per stale push.

The escape hatches are deliberate and all explicit:

  * `KG_STALE_ACK=<n>` where `<n>` is the total stale-file count this run
    reports. It names the number on purpose — it stops matching the moment the
    count moves, so it cannot be set once in a shell profile and forgotten.
  * `SKIP_KG=1` skips the check entirely, as it does at commit time.
  * `KG_AUTO_REFRESH=0` keeps the check and skips only the automatic refresh.
  * `git push --no-verify` skips every hook.

An unexpected error is never a block: `main` returns 0 on anything it did not
raise deliberately. A check that bricks pushes when it has a bug is worse than
no check.

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
import os
import re
import shutil
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


# --- content digest (verbatim copy of codebase_kg/writer.content_sha) --------
# Copied rather than imported: this file is vendored into repos that have no
# plugin install. tests/test_hook_parity.py asserts the two stay identical.
def content_sha(data: bytes) -> str:
    """SHA-256 of content, with CRLF folded to LF first."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def blob_digests(specs: list[str]) -> dict[str, str]:
    """SHA-256 of each `<rev>:<path>` blob, skipping any git cannot resolve.

    One `git cat-file --batch` for the whole set rather than a process per file:
    this runs while someone is waiting on a commit or a push, and a change set
    of a few hundred files is ordinary.

    The digest must be computed the same way `writer.content_sha` computes it,
    or every comparison reads as permanent drift — which is exactly what
    happened before `content_sha` folded CRLF: the builder hashes the working
    tree and this hashes the blob, and on a repo with `text=auto eol=lf` those
    differ for every text file on a Windows checkout.
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
        result[spec] = content_sha(out[pos : pos + size])
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


# --- staleness (verbatim copy of codebase_kg/staleness.py) -------------------
# Copied rather than imported, same as everything above. Drift here would mean
# the hooks and `kg_stats` disagree about how many files are stale, which is the
# exact failure the one-helper rule exists to prevent.
class Staleness(NamedTuple):
    """Every anchored file, split three ways."""

    stale: list[str]
    unbaselined: list[str]
    unreadable: list[str]


def classify(anchored, baselines, current):
    """Split every anchored path into stale / unbaselined / unreadable.

    Both digests must come from `content_sha`; hashing raw bytes on either side
    reports drift on every text file of a Windows checkout.
    """
    # Annotated, unlike the other vendored helpers: local annotations are never
    # evaluated, so they cost nothing here, and the parity test compares
    # statements — an unannotated copy reads as drift from the package's.
    stale: list[str] = []
    unbaselined: list[str] = []
    unreadable: list[str] = []
    for path in sorted(set(anchored)):
        now = current.get(path)
        recorded = baselines.get(path)
        if now is None:
            unreadable.append(path)
        elif not recorded:
            unbaselined.append(path)
        elif now != recorded:
            stale.append(path)
    return Staleness(stale, unbaselined, unreadable)


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
    nodes_by_path: dict[str, list[str]]  # which nodes a stale file puts in doubt


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
        # node_id alongside path in the same scan: a stale file is only worth
        # reporting because of the descriptions written against it, and a second
        # query for those would read the same table twice.
        nodes_by_path: dict[str, list[str]] = {}
        for node_id, raw in conn.execute("SELECT node_id, path FROM anchor"):
            nodes_by_path.setdefault(raw.replace("\\", "/"), []).append(node_id)
        paths = set(nodes_by_path)
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
        {p: sorted(set(ids)) for p, ids in nodes_by_path.items()},
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


def _root_to_rel(path: str, root: str) -> str:
    """An anchor path (relative to the graph's `root`) back to a repo-relative one.

    The inverse of `_rel_to_root`, needed because git only answers about
    repo-relative paths and anchors are never stored that way (SCHEMA.md §3).
    """
    root = norm_root(root)
    return f"{root}/{path}" if root else path


def repo_staleness(graph: Graph, root: str, revs: list[str]) -> Staleness:
    """Every anchored file in the repo against its baseline, read out of git.

    The question neither hook was asking. `analyze` compares a change set, so a
    file that drifts and is never re-derived is named once and then never again;
    this compares the whole map, so stale files cannot go quiet by being old.

    Read from `revs` rather than the working tree for the same reason the change
    set is: a push of a branch that is not checked out would otherwise be
    compared against whatever happens to be on disk.
    """
    anchored = sorted(graph.anchored)
    by_rel = {_root_to_rel(p, root): p for p in anchored}
    digests = digests_for(list(by_rel), revs)
    current = {by_rel[rel]: sha for rel, sha in digests.items()}
    return classify(anchored, graph.baselines, current)


def stale_nodes(graph: Graph, paths: list[str]) -> list[str]:
    """The nodes anchored on any of `paths` — what actually has to be re-read."""
    out: set[str] = set()
    for path in paths:
        out.update(graph.nodes_by_path.get(path, ()))
    return sorted(out)


def standing_line(stale: list[str], nodes: list[str], graph_rel: str) -> str:
    """The repo-wide total as one line, shared by both hooks.

    One line on purpose. The change-set report is already scoped and worth
    reading; the repo-wide total only needs to stop being invisible, and a
    second block at every commit would make people stop reading the first.
    """
    return (
        f"[codebase-kg] Repo-wide: {len(stale)} mapped file(s) and {len(nodes)} node(s) "
        f"no longer match what {graph_rel} was built against. Run /codebase-kg:audit."
    )


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


def _emit(
    findings: Findings, graph_rel: str, action: str = "push", blocked: bool = False
) -> None:
    """Report the findings on stderr.

    `action` names the change set being reported on. It is a parameter because
    this function is shared with the commit hook: hardcoding "push" made that
    hook announce a push that was not happening, and the fix at the time was a
    correcting line printed underneath — so every commit-time report contradicted
    its own header two lines later.

    `blocked` suppresses the two advisory lines for the same reason. These
    findings are still advisory when a push is blocked — the block comes from the
    stale files, reported separately below them — but a header promising the push is
    going through, three lines above one saying it is not, is the same defect.
    """
    unmapped, deleted, drifted = findings
    scope = "staged changes" if action == "commit" else "commits being pushed"
    header = f"[codebase-kg] Code-graph staleness check on the {scope}"
    msg = [
        header + "." if blocked else header + f" (advisory - your {action} is going through).",
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
    msg.append("[codebase-kg]   Run  /codebase-kg:refresh  to bring the graph back in line.")
    if not blocked:
        msg.append("[codebase-kg]   Nothing is blocked; this is a heads-up.")
    sys.stderr.write("\n".join(msg) + "\n")


def _emit_stale(
    stale: list[str],
    nodes: list[str],
    graph_rel: str,
    root: str,
    blocked: bool,
) -> None:
    """Report the repo-wide total, list every stale file, and give the verdict.

    Paths are converted back to repo-relative for display. They arrive relative
    to the graph's `root`, which is what the comparison needs and is not what
    the findings above them print — one report naming `ui/Known.kt` and
    `src/domain/Ranker.kt` for the same kind of thing reads as two different
    files.
    """
    msg = ["[codebase-kg]", standing_line(stale, nodes, graph_rel)]
    msg.append("[codebase-kg]")
    _block(
        msg,
        [_root_to_rel(p, root) for p in stale],
        f"{len(stale)} mapped file(s) no longer match the graph:",
        "~",
    )
    if not blocked:
        # `_block` closes with a separator for whatever follows it. Nothing does.
        while msg and msg[-1] == "[codebase-kg]":
            msg.pop()
        sys.stderr.write("\n".join(msg) + "\n")
        return
    msg += [
        "[codebase-kg] PUSH BLOCKED. A commit is provisional; a push is publication,",
        "[codebase-kg] and the graph no longer matches the code you are publishing.",
        "[codebase-kg]",
        "[codebase-kg]   Fix it:         /codebase-kg:audit, then /codebase-kg:refresh",
        f"[codebase-kg]   Accept it once: KG_STALE_ACK={len(stale)} git push ...",
        "[codebase-kg]   Skip the check: SKIP_KG=1 git push ...  (or git push --no-verify)",
        "[codebase-kg]",
        "[codebase-kg] The ack names the count on purpose: it stops matching as soon as",
        "[codebase-kg] the count moves, so it cannot be set once and forgotten.",
    ]
    sys.stderr.write("\n".join(msg) + "\n")


def acknowledged(stale: list[str]) -> bool:
    """Is this exact set of stale files already acknowledged for this push?

    `KG_STALE_ACK` must name the count. An ack that meant "yes, whatever the
    number" is the `--no-verify`-and-forget failure in a different spelling: set
    it once and the gate is off for good, including for every file that rots
    afterwards. Naming the number makes the acknowledgement expire on its own.
    """
    ack = os.environ.get("KG_STALE_ACK", "").strip()
    return ack.isdigit() and int(ack) == len(stale)


REFRESH_PROMPT = (
    "/codebase-kg:refresh This run is unattended and has no shell, so it cannot run git. "
    "The push hook already found the change set: the mapped files below no longer match "
    "the graph. Read them, re-derive the nodes that anchor on them, and write the graph "
    "only through the kg_* write tools (kg_upsert_node, kg_delete_node and the link and "
    "reference tools). Do not export, edit or build .kg-export.json."
)
REFRESH_TIMEOUT = 900

_REFRESH_MCP_TOOLS = (
    "kg_validate", "kg_stats", "kg_search", "kg_node", "kg_find_by_path",
    "kg_upsert_node", "kg_delete_node", "kg_add_link", "kg_remove_link",
    "kg_add_reference", "kg_remove_reference", "kg_neighborhood",
)

# Unattended, so narrower than the skill's own `allowed-tools`: the run reads the
# repo and writes only through the MCP write tools, which validate each change and
# touch nothing but the graph. No Bash at all: even read-only git takes
# `--output=<file>`. No Write, Edit or CLI runner either, so text in the repo
# cannot steer the run into writing or executing anything. The hook supplies the
# change set in the prompt instead. Both server names are listed: the host
# prefixes the plugin's server one way and a direct install another.
REFRESH_TOOLS = [
    f"{prefix}{name}"
    for prefix in ("mcp__codebase-kg__", "mcp__plugin_codebase-kg_codebase-kg__")
    for name in _REFRESH_MCP_TOOLS
] + ["Read(./**)", "Grep", "Glob"]


def refresh_prompt(stale: list[str]) -> str:
    return REFRESH_PROMPT + "\n\nStale mapped files:\n" + "\n".join(f"- {path}" for path in stale)

# Git exports GIT_DIR, GIT_INDEX_FILE and friends into hooks; passed on, they
# point the nested run's git at the pushing process's state. Everything else the
# run needs to start, authenticate and reach the API is named here.
_REFRESH_ENV = (
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USER", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMROOT", "SYSTEMDRIVE",
    "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TERM",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)


def refresh_env() -> dict[str, str]:
    upper = {k.upper(): v for k, v in os.environ.items()}
    env = {name: upper[name] for name in _REFRESH_ENV if name in upper}
    env["KG_REFRESHING"] = "1"
    return env


def can_auto_refresh(refs: list[tuple[str, str, str, str]], graph_rel: str) -> bool:
    """May the hook run the refresh itself for this push?

    The pushed tip has to be `HEAD`: the hook cannot add a commit to the push in
    progress, so the refreshed graph is committed on `HEAD` and lands with the
    next push. A graph file with uncommitted changes is someone's work, and the
    refresh would build on top of it.
    """
    if os.environ.get("KG_AUTO_REFRESH", "").strip() == "0":
        return False
    if os.environ.get("KG_REFRESHING"):
        return False
    if not shutil.which("claude"):
        return False
    head = _git("rev-parse", "HEAD").strip()
    if not head or head not in push_tips(refs):
        return False
    return not _git("status", "--porcelain", "--", graph_rel).strip()


def _run_claude(claude: str, repo: Path, stale: list[str]) -> str | None:
    """Run the refresh skill headless. Returns the failure, or None on exit 0.

    stdin is closed: the hook's own stdin carried the pushed refs and is spent.
    `KG_REFRESHING` stops a refresh that somehow pushes from refreshing again.
    """
    try:
        proc = subprocess.run(
            [claude, "-p", refresh_prompt(stale), "--allowedTools", ",".join(REFRESH_TOOLS)],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=REFRESH_TIMEOUT,
            env=refresh_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"claude did not finish within {REFRESH_TIMEOUT} seconds"
    except OSError as exc:
        return f"claude did not start: {exc}"
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])
        return f"claude exited with status {proc.returncode}" + (f": {tail}" if tail else "")
    return None


def _commit_graph(graph_rel: str) -> str | None:
    """Commit the graph file alone. Returns the short sha, or None on failure."""
    env = {**os.environ, "KG_REFRESHING": "1"}
    for cmd in (
        ["git", "add", "--", graph_rel],
        ["git", "commit", "-m", "Refresh the code graph", "--", graph_rel],
    ):
        if subprocess.run(cmd, capture_output=True, env=env, check=False).returncode != 0:
            return None
    return _git("rev-parse", "--short", "HEAD").strip() or None


def _may_auto_refresh(refs: list[tuple[str, str, str, str]], graph_rel: str) -> bool:
    try:
        return can_auto_refresh(refs, graph_rel)
    except Exception:  # noqa: BLE001 - the stale state is real; fall back to the block
        return False


def auto_refresh(repo: Path, graph_rel: str, root: str, stale: list[str]) -> str | None:
    """Refresh the graph with a headless run and commit it.

    Returns None once the refreshed graph is committed, else the reason it was
    not. Any graph change from a failed run stays uncommitted.
    """
    try:
        failure = _run_claude(
            shutil.which("claude") or "claude", repo, [_root_to_rel(p, root) for p in stale]
        )
        if failure:
            return failure
        fresh = read_graph(repo / graph_rel)
        if fresh is None:
            return "the refreshed graph is unreadable"
        left = repo_staleness(fresh, root, ["HEAD"]).stale
        if left:
            return f"{len(left)} mapped file(s) are still stale after the refresh"
        if not _git("status", "--porcelain", "--", graph_rel).strip():
            return "the refresh did not change the graph"
        sha = _commit_graph(graph_rel)
        if sha is None:
            return "git could not commit the refreshed graph"
    except Exception as exc:  # noqa: BLE001 - the block message still follows
        return f"the refresh raised: {exc}"
    sys.stderr.write(
        f"[codebase-kg] Graph refreshed and committed as {sha}. Review it with "
        f"`git show {sha}`, then run git push again: a pre-push hook cannot add a commit "
        "to the push in progress.\n"
    )
    return None


def main() -> int:
    """Run the check. Returns 1 only for unacknowledged stale mapped files.

    Every other outcome — no graph, an unreadable one, findings in the change
    set, or a bug in here — returns 0. A staleness check that can fail a push by
    crashing is worse than no staleness check.
    """
    try:
        return _run()
    except Exception as exc:  # noqa: BLE001 - never fail a push over this
        sys.stderr.write(f"[codebase-kg] staleness check did not run: {exc}\n")
        return 0


def _run() -> int:
    if os.environ.get("SKIP_KG", "").strip():
        return 0
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

    # The repo-wide pass, over every anchored file rather than the change set.
    revs = push_tips(refs) or ["HEAD"]
    split = repo_staleness(graph, root, revs)
    blocked = bool(split.stale) and not acknowledged(split.stale)

    if blocked and _may_auto_refresh(refs, graph_rel):
        sys.stderr.write(
            f"[codebase-kg] The graph is stale ({len(split.stale)} mapped file(s)). Running "
            '`claude -p "/codebase-kg:refresh"` to refresh it. This can take a few minutes and '
            "costs one headless model run. Set KG_AUTO_REFRESH=0 to turn it off.\n"
        )
        failure = auto_refresh(repo, graph_rel, root, split.stale)
        if failure is None:
            return 1
        sys.stderr.write(
            f"[codebase-kg] Auto-refresh failed: {failure}. "
            "Any change to the graph is left uncommitted.\n"
        )

    if any(findings):
        _emit(findings, graph_rel, blocked=blocked)
    if split.stale:
        _emit_stale(split.stale, stale_nodes(graph, split.stale), graph_rel, root, blocked)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())

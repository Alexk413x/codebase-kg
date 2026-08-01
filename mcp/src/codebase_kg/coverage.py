"""What the graph is *supposed* to cover, declared rather than inferred.

The old rule was "source extensions are whatever extensions the graph already
anchors on". That is genuinely language-agnostic, and it is also **silent by
construction**: a file type with zero coverage contributes zero extensions, so
it is never looked at, so it never produces a warning. On the RPN calculator all
142 anchors were `.kt`, so 34 XML files, 3 Gradle scripts, a version catalog and
a ProGuard config were invisible — not "reported as gaps", *invisible*. A blind
spot that conceals itself is worse than a known gap.

So coverage is declared. `meta.covers` says which files a node is expected to
exist for; `meta.exempt` subtracts the ones deliberately left out. Every file
under the root then lands in exactly one of four buckets:

    covered       matches `covers`, and some node anchors it
    gap           matches `covers`, nothing anchors it, not exempt  ← the report
    exempt        matches `covers`, nothing anchors it, but excused
    out_of_scope  does not match `covers` at all

There is no fifth "invisible" state. That is the whole point.

Patterns are gitignore-flavoured globs — `**/` spans path segments, `*` and `?`
stay within one, a trailing `/` means everything beneath. They are matched
against paths relative to the graph's `root`, posix-separated, exactly as
anchors are stored.

This module is stdlib-only and dependency-free on purpose: `git-hooks/
kg_pre_push.py` carries a verbatim copy of `glob_to_regex`/`compile_patterns`/
`matches_any` because it must run in repos with no plugin install, and
`tests/test_hook_parity.py` asserts the two stay identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Declared-coverage keys as they appear in the `meta` table and in the JSON
# interchange document. Stored newline-separated in SQL (one pattern per line),
# because a meta value is TEXT and a JSON array inside a TEXT column would need
# a parser in the vendored hook that deliberately has none.
COVERS_KEY = "covers"
EXEMPT_KEY = "exempt"


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


def declared_roots(patterns: list[str] | tuple[str, ...]) -> set[str]:
    """The directories a `covers` pattern names literally, before its first glob.

    A tree walk prunes conventionally-non-source directories (`.git`,
    `node_modules`, `.github`, …) so a large repo stays a cheap walk. That
    default must never be able to veto the graph's own declaration: a repo whose
    `covers` says `.githooks/*` has stated those files are source, and pruning
    the directory anyway drops them into a fifth bucket — walked past, never
    classified, invisible. That is precisely the failure this module exists to
    prevent, so the declaration wins and the prune yields.

    Ancestors are included because a walk prunes top-down: reaching
    `app/.generated/` means not pruning `app/` on the way.

    A pattern that opens with a wildcard (`**/*.py`) declares no literal
    directory and so lifts no prune — otherwise it would drag `.venv` and
    `node_modules` back into every walk.
    """
    out: set[str] = set()
    for raw in patterns:
        pat = raw.strip().replace("\\", "/")
        if not pat or pat.startswith("#"):
            continue
        segs = pat.rstrip("/").split("/")
        if not pat.endswith("/"):
            segs = segs[:-1]  # the last segment names a file, not a directory
        cur: list[str] = []
        for seg in segs:
            if not seg or seg in {".", ".."} or _GLOB_CHARS.search(seg):
                break
            cur.append(seg)
            out.add("/".join(cur))
    return out


_GLOB_CHARS = re.compile(r"[*?\[]")


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


def format_patterns(patterns: list[str]) -> str:
    """The storage form. Sorted + deduped so a rebuild is byte-stable."""
    return "\n".join(sorted(dict.fromkeys(p.strip() for p in patterns if p.strip())))


@dataclass
class CoverageReport:
    """The four buckets, plus whether anyone declared what should be in them."""

    declared: bool = False
    covered: int = 0
    gaps: list[str] = field(default_factory=list)
    exempt: int = 0
    out_of_scope: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "declared": self.declared,
            "covered": self.covered,
            "gap_count": len(self.gaps),
            "gaps": self.gaps,
            "exempt": self.exempt,
            "out_of_scope": self.out_of_scope,
        }
        if self.truncated:
            d["truncated"] = True
        if not self.declared:
            # The honest caveat, in the payload rather than in documentation
            # nobody reads at the moment they need it.
            d["warning"] = (
                "No `covers` declared, so coverage is inferred from the extensions "
                "already anchored — a file type with no coverage at all is invisible "
                "to this check. Declare `covers` in the graph's meta to get a real "
                "answer."
            )
        return d


def classify(
    files: list[str],
    anchored: set[str],
    covers: list[str],
    exempt: list[str],
    limit: int = 50,
) -> CoverageReport:
    """Sort `files` (relative to the graph root) into the four buckets.

    With no `covers`, falls back to the old inferred-extension rule so an
    existing graph keeps working — but says so, loudly, in `declared: False`.
    """
    covers_re = compile_patterns(covers)
    exempt_re = compile_patterns(exempt)
    report = CoverageReport(declared=covers_re is not None)

    if covers_re is None:
        exts = {_suffix(p) for p in anchored if _suffix(p)}
        in_scope = [f for f in files if _suffix(f) in exts] if exts else []
    else:
        in_scope = [f for f in files if matches_any(f, covers_re)]

    report.out_of_scope = len(files) - len(in_scope)
    for rel in in_scope:
        if rel in anchored:
            report.covered += 1
        elif matches_any(rel, exempt_re):
            report.exempt += 1
        elif len(report.gaps) < limit:
            report.gaps.append(rel)
        else:
            report.truncated = True
    return report


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""


def resolve_source_base(
    graph_dir: Path, root: str, rels: list[str], repo_root: str | Path | None = None
) -> Path | None:
    """The directory anchor paths resolve from, or None if none of them do.

    Anchors are stored relative to the graph's `root`, but where that root sits
    depends on how the caller got here — the MCP server knows only the graph
    file's location, the build CLI knows only the output path. Rather than
    encode a layout assumption, try the plausible bases and keep whichever
    actually resolves the most anchors. Sampling the first 25 is enough to
    distinguish a right base from a wrong one and keeps this off the critical
    path for a large graph.
    """
    if not rels:
        return None
    root = root.strip().rstrip("/")
    candidates: list[Path] = []
    if repo_root:
        rp = Path(repo_root)
        candidates += [rp / root, rp]
    candidates += [graph_dir / root, graph_dir.parent / root, graph_dir, graph_dir.parent]
    best: tuple[int, Path] | None = None
    for base in candidates:
        hits = sum(1 for r in rels[:25] if (base / r).is_file())
        if hits > 0 and (best is None or hits > best[0]):
            best = (hits, base)
    return best[1] if best else None

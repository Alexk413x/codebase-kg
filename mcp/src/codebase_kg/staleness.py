"""The one comparison that decides whether a mapped file has drifted. Stdlib only.

`kg_stats`, `kg_validate`, the pre-commit hook and the pre-push hook all report
staleness, and each of them used to work it out for itself. That is not a style
complaint. The digest being compared carries a rule inside it — `content_sha`
folds CRLF to LF, because the builder reads the working tree while the hooks read
the git blob, and on a Windows checkout those differ for every text file — and
the rule was visible only to whoever opened the hashing helper. A hand-rolled
check over raw `sha256(read_bytes())` reported 113 stale files where the truth
was 47; written back as baselines, those digests would have left every file it
"fixed" reading as drifted forever.

So the comparison lives here, once. `classify` is the whole rule: what counts as
stale, what counts as never-baselined, and what counts as unreadable. A caller
supplies two digest maps and gets the split back; there is nothing left for it to
decide differently.

The other half of the fix is scope. Both hooks compare a change set, which is
right for per-commit noise and wrong for a backlog: a file that drifts and is
never re-derived is reported once, in the commit that touched it, and never
again. Every function here takes the graph's *whole* anchored set.

`git-hooks/kg_pre_push.py` carries a verbatim copy of `Staleness` and `classify`
for the same reason it copies `content_sha` — it is vendored into repos with no
plugin install — and `tests/test_hook_parity.py` asserts the two agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from .writer import file_sha

#: How many paths and node ids a report lists before it just gives the count.
SAMPLE = 20

REFRESH_HINT = (
    "re-read these against the code with /codebase-kg:audit, then "
    "/codebase-kg:refresh — never re-baseline without reading"
)


# --- the rule, copied verbatim into git-hooks/kg_pre_push.py -----------------
class Staleness(NamedTuple):
    """Every anchored file, split three ways."""

    stale: list[str]
    unbaselined: list[str]
    unreadable: list[str]


def classify(
    anchored: Iterable[str],
    baselines: Mapping[str, str],
    current: Mapping[str, str],
) -> Staleness:
    """Split every anchored path into stale / unbaselined / unreadable.

    `anchored` is every path the graph anchors on, relative to the graph's root.
    `baselines` maps those paths to the digest recorded when the graph was built
    (SCHEMA.md §6.3). `current` maps them to the digest the source carries now;
    a path missing from it could not be read, which is never the same answer as
    unchanged.

    Both digests must come from `content_sha` — see this module's docstring for
    what happens when one side hashes raw bytes instead.
    """
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


# --- end verbatim copy -------------------------------------------------------


def digest_tree(anchored: Iterable[str], base: Path) -> dict[str, str]:
    """Digest each anchored file off the working tree, skipping what will not read.

    The MCP tools' side of `classify`. The hooks read git instead — the index at
    commit time, the pushed tips at push time — because a branch that is not
    checked out has no working tree to compare against.
    """
    out: dict[str, str] = {}
    for path in anchored:
        sha = file_sha(base / path)
        if sha is not None:
            out[path] = sha
    return out


def report(split: Staleness, nodes: list[str]) -> dict[str, Any]:
    """The `staleness` payload `kg_stats` and `kg_validate` both return.

    One shape from both tools, so an agent that orients with `kg_stats` and one
    that validates read the same numbers and can be compared.
    """
    return {
        "checked": True,
        "stale_files": len(split.stale),
        "stale_nodes": len(nodes),
        "files": split.stale[:SAMPLE],
        "nodes": nodes[:SAMPLE],
        "unbaselined_files": len(split.unbaselined),
        "unreadable_files": len(split.unreadable),
        "hint": REFRESH_HINT if split.stale else "",
    }


def unchecked(reason: str) -> dict[str, Any]:
    """The same payload when the source tree could not be located.

    Zeros with `checked: false`, never zeros alone: "nothing drifted" and "I
    could not look" are different answers and a count on its own conflates them.
    """
    return {
        "checked": False,
        "reason": reason,
        "stale_files": 0,
        "stale_nodes": 0,
        "files": [],
        "nodes": [],
        "unbaselined_files": 0,
        "unreadable_files": 0,
        "hint": "",
    }


__all__ = [
    "SAMPLE",
    "REFRESH_HINT",
    "Staleness",
    "classify",
    "digest_tree",
    "report",
    "unchecked",
]

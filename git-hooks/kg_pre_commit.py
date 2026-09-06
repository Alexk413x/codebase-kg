#!/usr/bin/env python3
"""codebase-kg staleness check over the STAGED change set. Advisory.

The same question `kg_pre_push.py` asks, asked one step earlier: does this
change add source no node covers, or delete source the graph still anchors on?
Finding out at commit is worth more than finding out at push, because the commit
that needs the graph update is still the one in front of you.

It does NOT refresh anything, and cannot. `/codebase-kg:refresh` maps changed
files to nodes, hands a JSON diff to a person to read, and decides what to add,
edit or remove -- judgement a shell hook has no way to make. Automating the
CHECK is the honest half; automating the fix would mean writing whatever the
hook guessed into the graph and calling it knowledge.

Everything is imported from `kg_pre_push.py` beside it rather than copied, so
there is one implementation of the coverage rule and only one file to keep in
step with `codebase_kg/coverage.py`.

It also closes a blind spot in the push-time check. That one compares against
the upstream branch, so once you have pushed it reports nothing -- which is
exactly when someone thinks to look. Staged-against-HEAD is always the right
comparison, whatever has been pushed.

Skip with `SKIP_KG=1`, or `git commit --no-verify` to skip every hook.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kg_pre_push import (  # noqa: E402
    _emit, _git, analyze, find_graph_rel, load_config, read_graph,
)


def staged_files() -> list[tuple[str, str]]:
    """`(status, path)` for what is staged, in `changed_files`' own shape.

    `--cached` against HEAD: the change set this commit will actually contain,
    which is not the same as the working tree and not the same as a push range.
    """
    out = _git("diff", "--cached", "--name-status")
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        # A rename arrives as `R100\told\tnew`; the new path is what a node
        # anchors on now.
        pairs.append((status, parts[-1].replace("\\", "/")))
    return pairs


def main() -> int:
    if os.environ.get("SKIP_KG", "").strip():
        return 0
    repo = Path(_git("rev-parse", "--show-toplevel").strip() or ".").resolve()
    cfg = load_config(repo)
    graph_rel = find_graph_rel(repo, cfg)
    if graph_rel is None:
        return 0
    loaded = read_graph(repo / graph_rel)
    if loaded is None:
        return 0
    graph_root, anchored, covers, exempt = loaded
    root = (cfg.get("root") or graph_root).replace("\\", "/").strip("/")

    changed = staged_files()
    if not changed:
        return 0
    unmapped, deleted = analyze(changed, root, graph_rel, anchored, covers, exempt)
    if unmapped or deleted:
        _emit(unmapped, deleted, graph_rel)
        # `_emit` is the vendored pre-push wording and says "your push is going
        # through". Say which change set this actually was, rather than editing
        # a file that has to stay byte-identical to upstream.
        print("[codebase-kg]   (staged changes; the commit is going through.)",
              file=sys.stderr)
        print("[codebase-kg]   Skip this check with SKIP_KG=1.", file=sys.stderr)
    return 0  # advisory, always


if __name__ == "__main__":
    sys.exit(main())

"""Parser for the pre-rewrite `KNOWLEDGE_GRAPH.md`. Stdlib only. **Migration only.**

This is the old `loader.py`, kept verbatim in behaviour because it is the only
way to read graphs written before the store rewrite. It is *not* on the query
path — nothing in the running server imports it. `migrate.py` uses it once per
repo and then the markdown file is history.

It stays tolerant of the legacy hand-written field names (`type`/`files`/
`details`/`deps`) because the graphs in the wild used them.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Anchor, Meta, Node

# A markdown table row: | a | b | ... |
_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
# A "key: value" header line.
_HEADER_LINE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
# A markdown heading.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# A token that looks like a node id (no spaces / parens / markdown).
_ID_TOKEN = re.compile(r"^[A-Za-z0-9_./-]+$")
# A markdown table separator cell: ---, :--, --:, :-:
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")

# Field-name aliases → canonical node field. Includes legacy Android-KG names.
_KEY_ALIASES: dict[str, str] = {
    "id": "id",
    "kind": "kind",
    "type": "kind",
    "anchors": "anchors",
    "files": "anchors",
    "summary": "description",
    "description": "description",
    "details": "description",
    "edges": "edges",
    "deps": "edges",
    "dependencies": "edges",
    "parity": "parity",
    "counterpart": "counterpart",
    "divergence": "divergence",
    # Per-node dates do not survive the rewrite (the artifact carries one
    # `generated` stamp) but they are still recognized so the row terminates
    # the node block correctly instead of being treated as stray prose.
    "updated": "_updated",
    "last_updated": "_updated",
    "last-updated": "_updated",
}

# Free-prose fields where a literal " #…" tail may be meaningful content.
_PROSE_FIELDS = {"description", "divergence"}


def _split_row(line: str) -> list[str] | None:
    m = _ROW.match(line)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|")]


def _strip_comment(value: str) -> str:
    # In header and non-prose values, '#' is only ever glued to a path
    # (`path#Symbol`, `KG.md#node-id`) — never preceded by whitespace — so a
    # "  # comment" tail is safe to drop.
    return re.sub(r"\s+#.*$", "", value).strip()


def _strip_backticks(token: str) -> str:
    return token.strip().strip("`").strip()


def _split_list(value: str) -> list[str]:
    """anchors / edges are comma-separated; drop prose placeholders."""
    out: list[str] = []
    for raw in value.split(","):
        tok = _strip_backticks(raw)
        if not tok or tok in {"—", "-", "–"}:
            continue
        if tok.startswith(("(", "*")):
            continue
        out.append(tok)
    return out


def _is_placeholder(value: str) -> bool:
    """True for a header value that means "not set".

    Real graphs write absence several ways — a `<template>` slot, a bare dash,
    or a path stub with `(none)` appended (`counterpart: ../ (none)`). All of
    them must read as None rather than as a path.
    """
    v = value.strip().lower()
    return (
        v.startswith("<")
        or v in {"none", "n/a", "-", "—", "–", ""}
        or "(none)" in v
        or v.endswith("(n/a)")
    )


def parse_meta(text: str) -> Meta:
    """Pull the config block. Prefer the first fenced code block; otherwise read
    `key: value` lines before the first `##` section."""
    lines = text.splitlines()
    block: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            if not in_fence:
                in_fence = True
                continue
            break  # end of first fence
        if in_fence:
            block.append(line)
        else:
            if line.startswith("## "):
                break
            block.append(line)

    meta = Meta()
    for line in block:
        m = _HEADER_LINE.match(line.strip())
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = _strip_comment(m.group(2))
        if not val or _is_placeholder(val):
            continue
        if key == "codebase":
            meta.codebase = val
        elif key == "root":
            meta.root = val
        elif key == "counterpart":
            meta.counterpart = val
        elif key == "language":
            meta.language = val
        elif key == "refreshed":
            meta.generated = val
        else:
            meta.extra[key] = val
    return meta


def _finalize(fields: dict[str, str], section: str) -> Node | None:
    if "id" not in fields:
        return None
    return Node(
        id=_strip_backticks(fields["id"]),
        kind=fields.get("kind", "").strip(),
        description=fields.get("description", "").strip(),
        anchors=[Anchor.parse(a) for a in _split_list(fields.get("anchors", ""))],
        edges=[e for e in _split_list(fields.get("edges", "")) if _ID_TOKEN.match(e)],
        section=section,
        # `.get(..., "") or None` already yields None for a missing key, so the
        # `if "x" in fields` guards these carried were doing nothing.
        parity=fields.get("parity", "").strip().lower() or None,
        counterpart=_strip_backticks(fields.get("counterpart", "")) or None,
        divergence=fields.get("divergence", "").strip() or None,
    )


def _wide_columns(cells: list[str]) -> list[str] | None:
    """A wide-table header row (`| id | kind | anchors | … |`) → its columns.

    Both table shapes exist in the wild: the Android graph is vertical (one
    key/value table per node), the iOS graph is wide (one row per node under a
    repeated header). Requiring three-plus columns starting with `id` keeps this
    from firing on a vertical `| id | some_node |` row, where the second cell is
    a value rather than a column name.
    """
    if len(cells) < 3 or cells[0].strip().lower() != "id":
        return None
    cols: list[str] = []
    for c in cells:
        canon = _KEY_ALIASES.get(c.strip().lower())
        if canon is None:
            return None
        cols.append(canon)
    return cols


def _align(cells: list[str], cols: list[str]) -> list[str]:
    """Fit a data row to the header width.

    A prose cell containing a literal `|` (`Bookmarks | Highlights`) splits into
    extra cells; the surplus is folded back into the prose column rather than
    truncating the row.
    """
    if len(cells) == len(cols):
        return cells
    if len(cells) < len(cols):
        return cells + [""] * (len(cols) - len(cells))
    if "description" not in cols:
        return cells[: len(cols)]
    di = cols.index("description")
    extra = len(cells) - len(cols)
    return cells[:di] + [" | ".join(cells[di : di + extra + 1])] + cells[di + extra + 1 :]


def parse_nodes(text: str) -> list[Node]:
    """Walk the doc collecting nodes, in either table shape.

    Vertical: a node starts at an `id` row and absorbs following alias rows
    until a blank line, a heading, or the next `id` row.
    Wide: a header row names the columns and each following row is one node.
    """
    nodes: list[Node] = []
    section = ""
    current: dict[str, str] | None = None
    wide: list[str] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            node = _finalize(current, section)
            if node is not None:
                nodes.append(node)
            current = None

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            flush()
            wide = None  # each section repeats its own header row
            if heading.group(1) == "###":
                section = heading.group(2).strip()
            continue

        cells = _split_row(line)
        if cells is None:
            flush()  # blank or prose line ends a node block
            continue
        if cells and all(_SEPARATOR_CELL.match(c) for c in cells):
            continue  # a formatter-inserted |---|---| row — not a terminator
        if len(cells) < 2:
            flush()  # malformed / not a kv row
            continue

        columns = _wide_columns(cells)
        if columns is not None:
            flush()
            wide = columns
            continue
        if wide is not None:
            row = _align(cells, wide)
            fields = {
                col: (val if col in _PROSE_FIELDS else _strip_comment(val))
                for col, val in zip(wide, row)
                if val.strip()
            }
            node = _finalize(fields, section)
            if node is not None:
                nodes.append(node)
            continue

        key = cells[0].strip().lower()
        canon = _KEY_ALIASES.get(key)
        # A value (esp. a summary) may contain literal '|' — common in prose like
        # "Bookmarks | Read Later | Highlights". Rejoin everything after the key
        # cell so internal pipes don't truncate the node mid-block.
        value = (
            " | ".join(c.strip() for c in cells[1:]).strip()
            if len(cells) > 2
            else cells[1].strip()
        )
        if canon is not None and canon not in _PROSE_FIELDS:
            value = _strip_comment(value)
        if canon == "id":
            flush()
            current = {"id": _strip_comment(cells[1].strip())}
        elif current is not None and canon is not None:
            current[canon] = value
        else:
            # a row that isn't a node field (e.g. a wide-table data row)
            flush()
    flush()
    return nodes


def parse(text: str) -> tuple[Meta, list[Node]]:
    return parse_meta(text), parse_nodes(text)


def load(path: str | Path) -> tuple[Meta, list[Node]]:
    return parse(Path(path).resolve().read_text(encoding="utf-8"))

"""Parse a KNOWLEDGE_GRAPH.md (markdown source of truth) into a `Graph`.

Stdlib only. Tolerant of the new schema (SCHEMA.md §4) *and* the legacy
Acme-Android field names (`type`/`files`/`details`/`deps`) so the parser works
against existing hand-written KGs during migration.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Graph, Header, Node

# A markdown table row: | a | b | ... |
_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
# A "key: value" header line.
_HEADER_LINE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
# A markdown heading.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# A token that looks like a node id (no spaces / parens / markdown).
_ID_TOKEN = re.compile(r"^[A-Za-z0-9_./-]+$")

# Field-name aliases → canonical node field. Includes legacy Android-KG names.
_KEY_ALIASES: dict[str, str] = {
    "id": "id",
    "kind": "kind",
    "type": "kind",
    "anchors": "anchors",
    "files": "anchors",
    "summary": "summary",
    "details": "summary",
    "description": "summary",
    "edges": "edges",
    "deps": "edges",
    "dependencies": "edges",
    "parity": "parity",
    "counterpart": "counterpart",
    "divergence": "divergence",
}

_HEADER_KEYS = {"codebase", "root", "counterpart", "language", "refreshed"}


def _split_row(line: str) -> list[str] | None:
    m = _ROW.match(line)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|")]


def _strip_header_comment(value: str) -> str:
    # Header values never contain "path#Symbol", so a "  # comment" tail is safe
    # to drop. Require whitespace before '#' so we never cut an inline anchor.
    return re.sub(r"\s+#.*$", "", value).strip()


def _strip_backticks(token: str) -> str:
    return token.strip().strip("`").strip()


def _split_list(value: str) -> list[str]:
    """anchors / edges are comma-separated; drop prose placeholders like
    '(none)' or '—'."""
    out: list[str] = []
    for raw in value.split(","):
        tok = _strip_backticks(raw)
        if not tok or tok in {"—", "-", "–"}:
            continue
        if tok.startswith("(") or tok.startswith("*"):
            continue
        out.append(tok)
    return out


def parse_header(text: str) -> Header:
    """Pull the config block. Prefer the first fenced code block; otherwise read
    `key: value` lines before the first `##` section."""
    lines = text.splitlines()
    block: list[str] = []
    in_fence = False
    found_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            if not in_fence:
                in_fence = True
                found_fence = True
                continue
            break  # end of first fence
        if in_fence:
            block.append(line)
        elif not found_fence:
            if line.startswith("## "):
                break
            block.append(line)

    header = Header()
    for line in block:
        m = _HEADER_LINE.match(line.strip())
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = _strip_header_comment(m.group(2))
        if not val or val.startswith("<"):  # template placeholder
            continue
        if key == "codebase":
            header.codebase = val
        elif key == "root":
            header.root = val
        elif key == "counterpart":
            header.counterpart = val
        elif key == "language":
            header.language = val
        elif key == "refreshed":
            header.refreshed = val
        else:
            header.extra[key] = val
    return header


def _finalize(fields: dict[str, str], section: str) -> Node | None:
    if "id" not in fields:
        return None
    return Node(
        id=_strip_backticks(fields["id"]),
        kind=fields.get("kind", "").strip(),
        anchors=_split_list(fields.get("anchors", "")),
        summary=fields.get("summary", "").strip(),
        edges=[e for e in _split_list(fields.get("edges", "")) if _ID_TOKEN.match(e)],
        parity=(fields["parity"].strip().lower() or None) if "parity" in fields else None,
        counterpart=_strip_backticks(fields["counterpart"]) or None
        if "counterpart" in fields
        else None,
        divergence=fields.get("divergence", "").strip() or None
        if "divergence" in fields
        else None,
        section=section,
    )


def parse_nodes(text: str) -> list[Node]:
    """Walk the doc collecting node key/value tables. A node starts at an `id`
    row and absorbs following alias rows until a blank line, a heading, or the
    next `id` row."""
    nodes: list[Node] = []
    section = ""
    current: dict[str, str] | None = None

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
            if heading.group(1) == "###":
                section = heading.group(2).strip()
            continue

        cells = _split_row(line)
        if cells is None:
            flush()  # blank or prose line ends a node block
            continue
        if len(cells) < 2:
            flush()  # malformed / not a kv row
            continue

        key = cells[0].strip().lower()
        canon = _KEY_ALIASES.get(key)
        # A value (esp. summary/details) may contain literal '|' — common in prose
        # like "Bookmarks | Read Later | Highlights". Rejoin everything after the
        # key cell so internal pipes don't truncate the node mid-block.
        value = " | ".join(c.strip() for c in cells[1:]).strip() if len(cells) > 2 else cells[1].strip()
        if key == "id" or (canon == "id"):
            flush()
            current = {"id": cells[1].strip()}
        elif current is not None and canon is not None:
            current[canon] = value
        else:
            # a row that isn't a node field (e.g. a wide-table data row)
            flush()
    flush()
    return nodes


def parse_graph(text: str, path: str = "") -> Graph:
    return Graph(header=parse_header(text), nodes=parse_nodes(text), path=path)


def load_graph(kg_path: str | Path) -> Graph:
    p = Path(kg_path).resolve()
    text = p.read_text(encoding="utf-8")
    return parse_graph(text, path=str(p))

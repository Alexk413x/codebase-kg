"""The JSON interchange shape. Stdlib only.

An agent cannot write SQLite with an editor, so JSON is how a graph is authored:
`/codebase-kg:build` and `/codebase-kg:refresh` produce this document and hand it
to `python -m codebase_kg.build`, which validates it and writes the store.

`export.py` emits the same shape, so a refresh is export → edit → build, and the
round trip is lossless.

The JSON is a *transport*, not a second source of truth. It is not committed;
`code_graph.db` is.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Anchor, Meta, Node

_SHA256 = re.compile(r"[0-9a-f]{64}")

# Keys of the top-level document that are graph config rather than node data.
_META_KEYS = ("codebase", "root", "counterpart", "language", "generated", "covers", "exempt")


class DecodeError(ValueError):
    """The JSON document is not a graph this can build."""


def to_dict(
    meta: Meta, nodes: list[Node], sources: dict[str, str] | None = None
) -> dict[str, Any]:
    # Both shapes are defined once, on the model. Restating the field list here
    # is how an added field silently stops round-tripping: a test that feeds
    # `to_dict` straight into `from_dict` cannot notice a field that `to_dict`
    # never emitted in the first place.
    doc: dict[str, Any] = dict(meta.to_dict())
    if meta.extra:
        doc["extra"] = dict(meta.extra)
    doc["nodes"] = [_node_to_dict(n) for n in nodes]
    # Carried through the round trip so a refresh keeps the baselines it did not
    # re-verify. Emitted last and keyed by path — it is machine-maintained data
    # an author reads past, not something anyone edits by hand.
    if sources:
        doc["sources"] = dict(sorted(sources.items()))
    return doc


def _node_to_dict(n: Node) -> dict[str, Any]:
    d = dict(n.to_dict())
    if not n.section:
        d.pop("section", None)
    return d


def from_dict(doc: Any) -> tuple[Meta, list[Node], dict[str, str]]:
    """Parse an interchange document. Raises `DecodeError` with a usable message.

    The messages matter more than usual here: the author is an agent that just
    generated the file, and a precise complaint ("node 3: `anchors` must be a
    list of strings") is what lets it fix the document rather than start over.
    """
    if not isinstance(doc, dict):
        raise DecodeError("top level must be an object")
    raw_nodes = doc.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DecodeError("`nodes` must be a list")

    meta = Meta(
        codebase=_str(doc, "codebase"),
        root=_str(doc, "root"),
        counterpart=_str(doc, "counterpart") or None,
        language=_str(doc, "language") or None,
        generated=_str(doc, "generated"),
        covers=_str_list(doc, "covers", "meta"),
        exempt=_str_list(doc, "exempt", "meta"),
    )
    extra = doc.get("extra")
    if isinstance(extra, dict):
        meta.extra = {str(k): str(v) for k, v in extra.items()}
    # Anything else at the top level is config the author invented; carry it
    # rather than dropping it silently. `_`-prefixed keys are the exception —
    # they are comments (the authoring template uses them) and are ignored.
    for key, value in doc.items():
        if key in _META_KEYS or key in {"nodes", "extra", "sources"} or str(key).startswith("_"):
            continue
        if isinstance(value, (str, int, float)):
            meta.extra.setdefault(str(key), str(value))

    raw_sources = doc.get("sources")
    sources: dict[str, str] = {}
    if isinstance(raw_sources, dict):
        # A malformed digest is dropped rather than rejected: a bad baseline is
        # recoverable (the next build re-hashes that file) whereas failing the
        # whole build over machine-written data the author never touched is not
        # a useful place to be strict.
        sources = {
            str(k).replace("\\", "/"): v
            for k, v in raw_sources.items()
            if isinstance(v, str) and _SHA256.fullmatch(v)
        }

    return meta, [_node_from_dict(raw, i) for i, raw in enumerate(raw_nodes)], sources


def _str(doc: dict[str, Any], key: str) -> str:
    value = doc.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DecodeError(f"`{key}` must be a string")
    return value.strip()


def _node_from_dict(raw: Any, index: int) -> Node:
    where = f"node {index}"
    if not isinstance(raw, dict):
        raise DecodeError(f"{where}: must be an object")
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise DecodeError(f"{where}: `id` is required and must be a non-empty string")
    where = f"node '{node_id}'"

    kind = raw.get("kind", "")
    if not isinstance(kind, str):
        raise DecodeError(f"{where}: `kind` must be a string")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise DecodeError(f"{where}: `description` must be a string")

    return Node(
        id=node_id.strip(),
        kind=kind.strip(),
        description=description.strip(),
        anchors=[Anchor.parse(a) for a in _str_list(raw, "anchors", where)],
        edges=_str_list(raw, "edges", where),
        section=_opt_str(raw, "section", where) or "",
        parity=_opt_str(raw, "parity", where),
        counterpart=_opt_str(raw, "counterpart", where),
        divergence=_opt_str(raw, "divergence", where),
    )


def _str_list(raw: dict[str, Any], key: str, where: str) -> list[str]:
    value = raw.get(key, [])
    if value in (None, ""):
        return []
    if isinstance(value, str):
        # A single string where a list belongs is a common slip; accept it
        # rather than failing a whole build over punctuation.
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DecodeError(f"{where}: `{key}` must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def _opt_str(raw: dict[str, Any], key: str, where: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecodeError(f"{where}: `{key}` must be a string")
    return value.strip() or None

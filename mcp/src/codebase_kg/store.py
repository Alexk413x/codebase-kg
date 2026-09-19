"""Read-only access to a committed `code_graph.db`. Stdlib only.

`CodeGraph` is a thin, query-shaped facade over one SQLite connection. It
deliberately does *not* materialize the whole graph into memory: opening the
file is constant-time regardless of graph size, which is the property the whole
store rewrite exists to get. Tools ask SQL for the rows they need.

Everything here is read-only — the file is opened `mode=ro` with `query_only`
on, so a bug in a tool cannot mutate a committed artifact.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from . import links
from .coverage import COVERS_KEY, EXEMPT_KEY, parse_patterns
from .links import ExternalLink
from .models import Anchor, Meta, Node
from .schema import READ_PRAGMAS, SCHEMA_VERSION, split_identifier

_WORD = re.compile(r"[A-Za-z0-9]+")
# Shortest token that is worth matching as a prefix (see `search_ids`).
_MIN_PREFIX = 3
# SQLite refuses more than 32766 bound parameters, so IN-lists are chunked.
_MAX_PARAMS = 30000


GRAPH_FILENAME = "code_graph.db"


def discover_graph(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) for `knowledge/code_graph.db`.

    The CLIs default to a relative path, so running one from a subdirectory
    reported no graph in a repo that plainly has one. The MCP server has always
    walked up; the commands a skill runs are the ones that need it most, since
    a skill's cwd is wherever the conversation left it.

    By convention the graph lives in `knowledge/` — there is no repo-root
    fallback, so a stray `code_graph.db` cannot be picked up by accident.
    """
    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        candidate = d / "knowledge" / GRAPH_FILENAME
        if candidate.is_file():
            return candidate
    return None


class StoreError(RuntimeError):
    """The file is not a usable code graph."""


def tokenize(query: str) -> list[str]:
    """Query text → search tokens, with CamelCase identifiers split.

    Mirrors the expansion the writer applies when building the FTS payload, so
    `VideoPlaybackService` typed by a user matches the same way it was indexed.
    """
    out = [p.lower() for word in _WORD.findall(query) for p in split_identifier(word)]
    return list(dict.fromkeys(out))


class CodeGraph:
    """One opened `code_graph.db`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise StoreError(f"no code graph at {self.path}")
        uri = f"{self.path.as_uri()}?mode=ro"
        try:
            # FastMCP may dispatch tools from more than one thread; the lock
            # below serializes access, so cross-thread use is safe.
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.Error as exc:  # pragma: no cover - depends on the OS
            raise StoreError(f"cannot open {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        for pragma in READ_PRAGMAS:
            self._conn.execute(pragma)
        self._check_schema()
        # Probed once, not per query. `external_link` was added without a schema
        # bump precisely so a graph written before it existed still opens, which
        # means every read of it has to be conditional -- and asking sqlite_master
        # once per node would put a table lookup in the hot path of every
        # hydration.
        self._has_links = links.has_link_table(self._conn)

    # ---------------------------------------------------------------- lifecycle
    def _check_schema(self) -> None:
        try:
            row = self._q("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreError(
                f"{self.path} is not a code graph database ({exc}). "
                "Rebuild it with /codebase-kg:build."
            ) from exc
        found = int(row["value"]) if row else 0
        if found != SCHEMA_VERSION:
            # The two directions have opposite remedies, and saying "rebuild"
            # for both sent anyone with a newer graph to regenerate a perfectly
            # good file with an older server — which reproduces the mismatch,
            # discards whatever the newer schema added, and looks like the graph
            # is at fault. Which side is behind decides who moves.
            if found < SCHEMA_VERSION:
                fix = (
                    f"Upgrade the graph in place, preserving every node and edge:\n"
                    f"    python -m codebase_kg.upgrade \"{self.path}\""
                )
            else:
                fix = (
                    "This graph was written by a newer codebase-kg than the one "
                    "serving it. Update the plugin (/plugin), then reload — do not "
                    "rebuild, which would silently drop whatever the newer schema "
                    "records."
                )
            raise StoreError(
                f"{self.path} is schema v{found}, this server speaks "
                f"v{SCHEMA_VERSION}. {fix}"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _q(self, sql: str, params: tuple[object, ...] | dict[str, object] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # -------------------------------------------------------------------- meta
    @property
    def meta(self) -> Meta:
        rows = {r["key"]: r["value"] for r in self._q("SELECT key, value FROM meta")}
        known = {
            "schema_version", "codebase", "root", "counterpart", "language",
            "generated", COVERS_KEY, EXEMPT_KEY, links.NODE_TABLE_KEY,
        }
        return Meta(
            codebase=rows.get("codebase", ""),
            root=rows.get("root", ""),
            counterpart=rows.get("counterpart"),
            language=rows.get("language"),
            generated=rows.get("generated", ""),
            covers=parse_patterns(rows.get(COVERS_KEY)),
            exempt=parse_patterns(rows.get(EXEMPT_KEY)),
            extra={k: v for k, v in rows.items() if k not in known},
        )

    # ------------------------------------------------------------------- nodes
    def _in_chunks(self, sql: str, ids: list[str]) -> list[sqlite3.Row]:
        """Run `sql` (containing a single `{marks}` slot) over `ids`, chunked.

        SQLite hard-caps bound parameters at 32,766. Passing a whole graph's ids
        as an IN-list therefore *fails outright* past that size — which is well
        inside the range this store is meant to handle.
        """
        out: list[sqlite3.Row] = []
        for i in range(0, len(ids), _MAX_PARAMS):
            chunk = ids[i : i + _MAX_PARAMS]
            marks = ",".join("?" * len(chunk))
            out += self._q(sql.format(marks=marks), tuple(chunk)).fetchall()
        return out

    def _hydrate(
        self, rows: list[sqlite3.Row], *, with_edges: bool = True, whole_graph: bool = False
    ) -> list[Node]:
        """Rows from `node` → `Node`s, with anchors and edges fetched in bulk.

        A fixed number of queries regardless of how many nodes were asked for —
        the point being that a caller never pays per-node round trips.

        `whole_graph` drops the IN-list entirely (the answer is "all rows", so
        naming every id is both pointless and impossible past the parameter
        cap). `with_edges=False` skips the edge query for the callers that never
        read `Node.edges` — it is the most expensive of the three.
        """
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        anchors: dict[str, list[Anchor]] = {i: [] for i in ids}
        if whole_graph:
            anchor_rows = self._q(
                "SELECT node_id, path, symbol FROM anchor ORDER BY node_id, ord"
            ).fetchall()
        else:
            anchor_rows = self._in_chunks(
                "SELECT node_id, path, symbol FROM anchor WHERE node_id IN ({marks})"
                " ORDER BY node_id, ord",
                ids,
            )
        for a in anchor_rows:
            anchors[a["node_id"]].append(Anchor(path=a["path"], symbol=a["symbol"]))

        # One query for the whole batch, like anchors and edges -- a caller must
        # never pay per-node round trips just because a few nodes link out.
        outbound: dict[str, list[ExternalLink]] = {i: [] for i in ids}
        if self._has_links:
            link_rows = (
                self._q(
                    f"SELECT node_id, target, kind FROM {links.TABLE}"
                    " ORDER BY node_id, target, kind"
                ).fetchall()
                if whole_graph
                else self._in_chunks(
                    f"SELECT node_id, target, kind FROM {links.TABLE}"
                    " WHERE node_id IN ({marks}) ORDER BY node_id, target, kind",
                    ids,
                )
            )
            for r in link_rows:
                outbound[r["node_id"]].append(
                    ExternalLink(target=r["target"], kind=r["kind"])
                )

        edges: dict[str, list[str]] = {i: [] for i in ids}
        if with_edges:
            edge_rows = (
                self._q("SELECT src, dst FROM edge ORDER BY src, dst").fetchall()
                if whole_graph
                else self._in_chunks(
                    "SELECT src, dst FROM edge WHERE src IN ({marks}) ORDER BY src, dst", ids
                )
            )
            for e in edge_rows:
                edges[e["src"]].append(e["dst"])

        return [
            Node(
                id=r["id"],
                kind=r["kind"],
                description=r["description"],
                anchors=anchors[r["id"]],
                edges=edges[r["id"]],
                section=r["section"],
                parity=r["parity"],
                counterpart=r["counterpart"],
                divergence=r["divergence"],
                links=outbound[r["id"]],
            )
            for r in rows
        ]

    def node(self, node_id: str) -> Node | None:
        rows = self._q("SELECT * FROM node WHERE id = ?", (node_id,)).fetchall()
        hydrated = self._hydrate(rows)
        return hydrated[0] if hydrated else None

    def nodes(self, ids: list[str], *, with_edges: bool = True) -> list[Node]:
        if not ids:
            return []
        rows = self._in_chunks("SELECT * FROM node WHERE id IN ({marks})", ids)
        rows.sort(key=lambda r: r["id"])
        return self._hydrate(rows, with_edges=with_edges)

    def all_nodes(self) -> list[Node]:
        """Every node. Only for whole-graph passes (validate, export)."""
        return self._hydrate(
            self._q("SELECT * FROM node ORDER BY id").fetchall(), whole_graph=True
        )

    def node_summaries(self) -> list[tuple[str, str, str | None]]:
        """`(id, description, counterpart)` for every node, without hydrating.

        `kg_validate` reads only these three columns; building the full object
        graph — anchors, edges, `Node` instances — to look at three strings is
        the bulk of its cost on a large graph.
        """
        return [
            (r["id"], r["description"], r["counterpart"])
            for r in self._q("SELECT id, description, counterpart FROM node ORDER BY id")
        ]

    def inbound_many(self, node_ids: list[str]) -> dict[str, list[str]]:
        """`{dst: [src, …]}` for several nodes in one query."""
        if not node_ids:
            return {}
        rows = self._in_chunks(
            "SELECT src, dst FROM edge WHERE dst IN ({marks}) ORDER BY dst, src", node_ids
        )
        out: dict[str, list[str]] = {i: [] for i in node_ids}
        for r in rows:
            out[r["dst"]].append(r["src"])
        return out

    def inbound(self, node_id: str) -> list[str]:
        return [
            r["src"]
            for r in self._q("SELECT src FROM edge WHERE dst = ? ORDER BY src", (node_id,))
        ]

    # ------------------------------------------------------------------ search
    def search_ids(self, query: str, limit: int) -> list[tuple[str, float]]:
        """FTS5 hits as `(node_id, relevance)`, best first.

        Tokens of three or more characters are matched as prefixes so a partial
        identifier still finds its node. Shorter ones are matched exactly: a
        prefix search for "to" hits "tonight", "token" and "tools", which is
        pure noise rather than a partial identifier anyone typed on purpose.

        bm25 returns a negative score where more-negative is better; it is
        flipped here so callers can treat bigger as better.
        """
        tokens = tokenize(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"*' if len(t) >= _MIN_PREFIX else f'"{t}"' for t in tokens)
        try:
            rows = self._q(
                "SELECT node_id, bm25(node_fts) AS rank FROM node_fts"
                " WHERE node_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # malformed MATCH expression — treat as no hits
        return [(r["node_id"], -float(r["rank"])) for r in rows]

    def similar_ids(self, text: str, limit: int = 5) -> list[str]:
        """Ids that look like `text` — the `did_you_mean` list for a bad id.

        FTS first: it is indexed, and this is the path an agent hits whenever it
        guesses an id wrong. The `LIKE` fallback scans every row, so it only
        runs when the index found nothing.
        """
        hits = [nid for nid, _ in self.search_ids(text, limit)]
        if hits:
            return hits
        rows = self._q(
            "SELECT id FROM node WHERE id LIKE ? ORDER BY length(id), id LIMIT ?",
            (f"%{text.strip()}%", limit),
        ).fetchall()
        return [r["id"] for r in rows]

    def by_kind(self, kind: str) -> list[Node]:
        # Substring matching cannot use an index, so the scan is unavoidable —
        # but scanning `id` alone and hydrating the survivors beats decoding all
        # seven columns of every row. LIKE is already case-insensitive for
        # ASCII in SQLite, so `lower()` on the column bought nothing.
        ids = [
            r["id"]
            for r in self._q(
                "SELECT id FROM node WHERE kind LIKE ? ORDER BY id", (f"%{kind}%",)
            )
        ]
        return self.nodes(ids)

    def by_path(self, path: str) -> list[tuple[Node, list[Anchor]]]:
        """Nodes anchored on a source path, with the anchors that matched.

        `path` matches the full repo-relative path or any suffix of it on a
        segment boundary, so `FeedRanker.kt` finds `domain/FeedRanker.kt`.

        The filename is an indexed equality probe, and every possible match
        shares the needle's own filename — so narrowing on `base` first and
        applying the suffix rule to those few rows gives the same answers as
        `path LIKE '%/needle'` without that pattern's full scan of `anchor`.
        """
        needle = path.replace("\\", "/").strip().lstrip("./")
        rows = self._q(
            "SELECT node_id, path, symbol FROM anchor WHERE base = ? ORDER BY node_id, ord",
            (needle.rsplit("/", 1)[-1],),
        ).fetchall()
        matched: dict[str, list[Anchor]] = {}
        for r in rows:
            stored = r["path"]
            if stored != needle and not stored.endswith("/" + needle):
                continue
            matched.setdefault(r["node_id"], []).append(
                Anchor(path=stored, symbol=r["symbol"])
            )
        if not matched:
            return []
        nodes = {n.id: n for n in self.nodes(list(matched))}
        return [(nodes[i], matched[i]) for i in sorted(matched) if i in nodes]

    # ---------------------------------------------------- cross-graph links
    def external_links(self) -> list[tuple[str, ExternalLink]]:
        """`(node_id, link)` for the whole graph — for export and validation.

        Empty for a graph written before `external_link` existed, which reads as
        "no cross-graph links" rather than as an error. That is what makes the
        table additive: adoption never invalidates a committed artifact.
        """
        if not self._has_links:
            return []
        with self._lock:
            return links.all_links(self._conn)

    def nodes_linking_to(self, target: str, *, kind: str | None = None) -> list[str]:
        """Which nodes here point at that node over there? — the reverse lookup.

        Served by `external_link_target`. This is what makes a one-sided link
        answer both questions: "which screen does this code present?" is a row,
        and "which code presents this screen?" is this query.

        Accepts a full `<db-file>#<node-id>` or a bare peer node id, since the
        peer's own tools hand back the latter.
        """
        if not self._has_links:
            return []
        with self._lock:
            return links.nodes_linking_to(self._conn, target, kind=kind)

    def parity_nodes(self) -> list[Node]:
        rows = self._q(
            "SELECT * FROM node WHERE parity IS NOT NULL"
            " AND (parity = 'divergent' OR parity LIKE '%-only') ORDER BY id"
        ).fetchall()
        return self._hydrate(rows, with_edges=False)

    # -------------------------------------------------------------- traversal
    def neighborhood_ids(self, node_id: str, depth: int) -> dict[str, int]:
        """`{neighbor_id: hops}` within `depth` hops, following edges either way."""
        # Two recursive terms, not one over a UNION-ALL subquery. SQLite cannot
        # flatten a compound subquery into a join, so the single-term form built
        # an automatic covering index over every edge on *every call* — 133 ms
        # vs 0.24 ms at 30k nodes. Written this way each term uses edge's
        # primary key and the edge_dst index directly.
        rows = self._q(
            "WITH RECURSIVE nb(id, depth) AS ("
            "  SELECT :id, 0"
            "  UNION SELECT e.dst, nb.depth + 1 FROM nb JOIN edge e"
            "    ON e.src = nb.id WHERE nb.depth < :depth"
            "  UNION SELECT e.src, nb.depth + 1 FROM nb JOIN edge e"
            "    ON e.dst = nb.id WHERE nb.depth < :depth"
            ") SELECT id, MIN(depth) AS d FROM nb GROUP BY id",
            {"id": node_id, "depth": depth},
        ).fetchall()
        return {r["id"]: int(r["d"]) for r in rows}

    # ------------------------------------------------------------------ counts
    def counts(self) -> dict[str, int]:
        row = self._q(
            "SELECT (SELECT count(*) FROM node) AS nodes,"
            " (SELECT count(*) FROM edge) AS edges,"
            " (SELECT count(*) FROM anchor) AS anchors,"
            " (SELECT count(DISTINCT path) FROM anchor) AS files"
        ).fetchone()
        return {k: int(row[k]) for k in ("nodes", "edges", "anchors", "files")}

    def group_counts(self, column: str) -> dict[str, int]:
        """`{value: count}` over one node column, biggest first."""
        if column not in {"kind", "section", "parity"}:
            raise ValueError(f"not a groupable column: {column}")
        rows = self._q(
            f"SELECT COALESCE(NULLIF({column}, ''), '(none)') AS v, count(*) AS n"
            f" FROM node GROUP BY v ORDER BY n DESC, v"
        ).fetchall()
        return {r["v"]: int(r["n"]) for r in rows}

    def anchor_paths(self) -> list[str]:
        return [r["path"] for r in self._q("SELECT DISTINCT path FROM anchor ORDER BY path")]

    def sources(self) -> dict[str, str]:
        """`path -> sha` for every anchored file that has a recorded baseline.

        The baselines a build carries forward, and what `kg_validate` compares
        the working tree against. Empty for a graph built with no source tree in
        reach — which reads as "no baseline", never as "nothing changed".
        """
        return {r["path"]: r["sha"] for r in self._q("SELECT path, sha FROM source ORDER BY path")}

    def node_ids_for_paths(self, paths: list[str]) -> list[str]:
        """Every node anchored on any of `paths`, sorted.

        The second half of a staleness answer: a stale *file* only matters
        because of the descriptions written against it, and those are what has
        to be re-read. Indexed on `anchor_path` and chunked, so asking about a
        whole graph's worth of paths is one bounded set of queries rather than
        one per file.
        """
        if not paths:
            return []
        rows = self._in_chunks(
            "SELECT DISTINCT node_id FROM anchor WHERE path IN ({marks})", list(paths)
        )
        return sorted({r["node_id"] for r in rows})

    def all_anchors(self) -> list[tuple[str, Anchor]]:
        """Every anchor, **ordered by path** so a caller reading the source can
        finish one file before moving to the next — one read per file, and only
        one file's text alive at a time."""
        return [
            (r["node_id"], Anchor(path=r["path"], symbol=r["symbol"]))
            for r in self._q(
                "SELECT node_id, path, symbol FROM anchor ORDER BY path, node_id, ord"
            )
        ]

    def isolated_ids(self, limit: int | None = None) -> list[str]:
        """Nodes with no edge in either direction — usually a missed relationship."""
        sql = (
            "SELECT id FROM node WHERE id NOT IN (SELECT src FROM edge)"
            " AND id NOT IN (SELECT dst FROM edge) ORDER BY id"
        )
        if limit is not None:
            return [r["id"] for r in self._q(sql + " LIMIT ?", (limit,))]
        return [r["id"] for r in self._q(sql)]

    def isolated_count(self) -> int:
        return int(
            self._q(
                "SELECT count(*) AS n FROM node WHERE id NOT IN (SELECT src FROM edge)"
                " AND id NOT IN (SELECT dst FROM edge)"
            ).fetchone()["n"]
        )

    def is_anchored(self, path: str) -> bool:
        """Does any node anchor on this exact repo-relative path? Indexed probe."""
        return (
            self._q("SELECT 1 FROM anchor WHERE path = ? LIMIT 1", (path,)).fetchone()
            is not None
        )

# codebase-kg MCP server

A local **stdio** MCP server over one repo's committed `knowledge/code_graph.db` (see
[`../SCHEMA.md`](../SCHEMA.md)), exposing **nine typed queries and four targeted writes** — so an
agent answers "where does X live / what depends on it / what diverges from the peer" in one tool
call instead of re-grepping every session, and fixes one wrong description without regenerating the
whole artifact.

Mirrors the author's `a11y-kg` server (FastMCP, `uvx`-run). Everything except `server` is **stdlib
only** — the whole graph layer is testable without FastMCP installed.

## Queries

| Tool | Returns |
|---|---|
| `kg_search(query, kind?)` | Ranked FTS5 search over id / kind / description / anchors. CamelCase identifiers match in split form ("video playback" finds `VideoPlaybackService`). Capped at 10. |
| `kg_node(id)` | One full node — anchors, description, edges, parity, counterpart, divergence, inbound edges. `did_you_mean` on miss. |
| `kg_find_by_path(path)` | **Reverse lookup**: which node(s) own a source file, and what connects to them. Accepts a repo-relative path or a bare filename. |
| `kg_neighborhood(id, depth=1)` | A node + everything within 1–3 hops, following edges either way, with hop counts. |
| `kg_find_by_kind(kind)` | All nodes whose free-text `kind` matches (substring). |
| `kg_parity_gaps(status?)` | Nodes flagged `divergent` / `<codebase>-only` — the gap report as a query. |
| `kg_stats()` | Counts by kind / section / parity, edge and anchor totals, isolated nodes, `generated` date, and `staleness` — the repo-wide count of mapped files that no longer match what the graph was built against, with the nodes that describe them. |
| `kg_find_by_link(target)` | **Reverse lookup across graphs**: which code node(s) point at a node in another committed graph in this repo. |
| `kg_find_by_reference(query?, kind?)` | **Reverse lookup by documentation**: which node(s) cite a URL or title matching `query`, with the `path` / `symbol` each citation narrows to. |
| `kg_validate()` | Advisory drift check against real source: ungreppable anchors, uncovered source files, counterpart problems. Never blocks. |

## Writes

For **targeted** changes — a handful of nodes, a link. Bulk work stays on the round trip below, and
each tool's description says so, because picking the wrong one is how this surface gets misused.

| Tool | Does |
|---|---|
| `kg_upsert_node(nodes)` | Creates or updates node(s). Only the keys supplied change; `null` clears a parity field; `anchors` / `edges` / `external_links` / `references` replace the whole list. |
| `kg_delete_node(ids, dry_run=True, cascade_inbound=False)` | Previews what cascades (anchors, outbound edges, external links) and what blocks (inbound edges, `ON DELETE RESTRICT`), then deletes. |
| `kg_add_link(node_id, target, kind='')` | Points a node at `<db-file>#<node-id>` in another committed graph. Refused if the peer graph is present and lacks that node. |
| `kg_remove_link(node_id, target)` | Drops one such pointer; the node is untouched. |
| `kg_add_reference(node_id, url, kind='', title='', path=None, symbol=None)` | Records where a fact the node depends on is documented. `path` / `symbol` must equal one of the node's anchors, or the write is refused. |
| `kg_remove_reference(node_id, url, path=None, symbol=None)` | Drops the node's reference(s) to `url`, or only the one with that narrowing. |

Three properties, all in `edits.py`:

- **Atomic.** The mutation runs against a private copy of the file inside one transaction, and the
  copy replaces the original only at the very end. A rejected edit leaves the committed graph
  byte-identical — not rolled back, never opened for writing.
- **Validated, not merely constrained.** `kg_validate` — the same function the tool calls — runs
  against the copy, and the write is refused if it introduced a finding the graph did not already
  have. The test is *no new findings*, never *clean*: a real graph carries findings, and demanding
  zero would lock the tools out of the graphs that need editing.
- **Reported.** Every call returns the rows it touched, field by field, before and after. That is
  the diff review the export path gave for free.

## CLIs

Whole-graph writes — migration, and the bulk round trip:

```sh
python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md   # one-time: markdown → db
python -m codebase_kg.export -o .kg-export.json                   # db → JSON
python -m codebase_kg.build .kg-export.json -o knowledge/code_graph.db
rm .kg-export.json                                                # a snapshot, not a source
```

`export` → `build` with no edits is byte-identical, so a no-op refresh leaves the git diff empty.

## How it finds the graph

Path resolution order:

1. First CLI arg (e.g. `codebase-kg /path/to/code_graph.db`).
2. `$CODEBASE_KG_PATH`.
3. Walk up from the current working directory, honoring an optional `graph_path` override in
   `.claude/codebase-kg.local.md` (SCHEMA.md §8 — same file the hooks read; the older `kg_path` key
   is still accepted), else `knowledge/code_graph.db`. **No repo-root fallback.**

While unresolved, the path is re-resolved on every tool call, so a graph created after the server
started (e.g. by `/codebase-kg:build`) is picked up without a restart. If the repo still has a
pre-0.2 `KNOWLEDGE_GRAPH.md`, the error names the migration command rather than just saying "not
found".

The graph is **opened per tool call and closed again**. That is affordable because opening a store
is constant-time (~1 ms) rather than a parse whose cost grows with the graph — there is nothing to
amortize. It also matters on Windows, where a held-open handle blocks the file from being replaced:
caching the connection would make `/codebase-kg:refresh` fail to write its own output whenever the
server was running.

## Module map

| Module | Role |
|---|---|
| `schema.py` | The DDL. Constraints make dangling edges, duplicate ids, inconsistent parity and line-number anchors unwritable. |
| `models.py` | `Node` / `Anchor` / `Meta` dataclasses — the write shape. |
| `clean.py` | The `description` contract (no ticket refs, dates or change narrative) and its scrubber. |
| `writer.py` | Transactional, deterministic build. Validates first; writes to a temp file and renames. |
| `store.py` | `CodeGraph` — read-only query facade over one connection. |
| `tools.py` | The read-only queries the MCP tools wrap. |
| `edits.py` | Targeted writes: copy, mutate in one transaction, validate, swap in — or discard, leaving the committed file byte-identical. |
| `codec.py` | The JSON interchange shape shared by `build` and `export`. |
| `markdown.py` | The pre-0.2 markdown parser. **Migration only** — nothing on the query path imports it. |

## Development

```sh
uv sync
uv run pytest -q
```

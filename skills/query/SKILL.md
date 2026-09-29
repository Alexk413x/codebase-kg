---
name: query
description: This skill should be used to find or orient in code in a repo that has a knowledge/code_graph.db — when the user asks "where does X live", "what handles X", "what depends on X", "how is X wired", "show me the X code", "orient me in this codebase", "what would break if I change X", or when a codebase-kg search gate has denied a Grep/Glob and told you to query the graph first. It answers WHERE from the committed graph, then reads the anchored source to confirm what the code currently does. (To change the graph use refresh; to check whether the graph is still accurate use validate or audit.)
allowed-tools:
  # Both names the host gives the server: bare when the MCP server is installed
  # directly, prefixed when it arrives as a plugin.
  - mcp__codebase-kg__kg_search
  - mcp__codebase-kg__kg_node
  - mcp__codebase-kg__kg_neighborhood
  - mcp__codebase-kg__kg_find_by_path
  - mcp__codebase-kg__kg_find_by_kind
  - mcp__codebase-kg__kg_find_by_link
  - mcp__codebase-kg__kg_find_by_reference
  - mcp__codebase-kg__kg_stats
  - mcp__plugin_codebase-kg_codebase-kg__kg_search
  - mcp__plugin_codebase-kg_codebase-kg__kg_node
  - mcp__plugin_codebase-kg_codebase-kg__kg_neighborhood
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_path
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_kind
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_link
  - mcp__plugin_codebase-kg_codebase-kg__kg_find_by_reference
  - mcp__plugin_codebase-kg_codebase-kg__kg_stats
  - Read
  - Grep
  - Glob
---

# query — find code through the graph, confirm it in the source

The graph is a committed map of this codebase. Use it to answer **where** something lives, then read
the anchored files to answer **what it does now**. That split is the whole contract:

- **The graph is authoritative for structure.** It knows the components a search string does not
  appear in — the class that handles ranking without the word "rank" in it, the module a feature
  depends on three hops away.
- **The source is authoritative for behavior.** The graph is a snapshot taken at build time. A
  description can be stale; the file cannot.

A text search reverses both. It finds strings, not components, and it re-derives a map that already
exists.

## If a search gate sent you here

In a repo that has a graph, the plugin denies a `Grep`/`Glob` (or shell `grep`/`rg`/`find -name`)
aimed at mapped code until a codebase-kg query earns credit. One query clears the next few searches
(`gate_credit`, default 3); when the credit runs out, the gate denies again. A search scoped to a
file the graph anchors is never gated. The denial is this workflow's cue, not an obstacle. Run
step 1, and:

- **The graph answers it** → carry on from step 2. You are done faster than the grep would have been.
- **The graph does not cover it** → say so and run the same search again. A repeat of a denied
  search always passes, however you reword the command. Then treat the miss as a finding: a hole in
  the map is worth reporting, and `/codebase-kg:refresh` is what fills it.

Never report "the graph has nothing" without having run `kg_search` with more than one phrasing.

## Workflow

### 1. Locate
Start with **`kg_search`** — a persisted FTS5 index over ids, kinds, descriptions and anchors, with
CamelCase split, so `video playback` finds `VideoPlaybackService`. Search the concept, not the
identifier you are guessing at.

When you already have a more specific handle, go straight to it:

| You have | Use |
|---|---|
| a file path from a stack trace, diff or hook report | `kg_find_by_path` |
| a node id | `kg_node` |
| a category ("every ViewModel", "every migration") | `kg_find_by_kind` |
| a node in a paired repo's graph | `kg_find_by_link` |
| a platform API, spec or doc URL that moved | `kg_find_by_reference` |
| no idea of the shape of the repo | `kg_stats` first — sections and kinds are the table of contents |

### 2. Expand
**`kg_neighborhood`** from the node you landed on. This is the step that makes the graph worth
having: it returns what the node points at and what points back, so "what breaks if I change this"
is one call rather than a recursive grep. Widen the hops only while each hop is still relevant.

### 3. Confirm in source
`Read` the files the anchors name. Anchors are `path#Symbol` relative to the graph's `root`
(`kg_stats` reports it), never line numbers, so they survive edits above them.

**Read before you answer.** If a description and the code disagree, the code wins and the description
is a finding.

### 4. Answer, and say what is graph and what is source
Cite the anchors you actually read. Where you are repeating a node description without having opened
the file, say so — that is the difference between "the map says" and "the code does".

## Report a stale map

Anything you hit along the way is worth one line at the end:

- an anchor whose symbol is no longer in the file, or whose file is gone;
- a description that no longer matches what you read;
- a file with no node at all.

Point at `/codebase-kg:refresh` for the fix, `/codebase-kg:validate` for a full deterministic sweep.
Do not fix the graph from inside this skill — this one only reads.

## Rules

- **Do not paste code into the answer** beyond the few lines that carry the point. Point at
  `path#Symbol`; the reader has the repo.
- **Do not treat the graph as the last word on behavior.** It is a snapshot. Every behavioral claim
  in your answer traces to a file you opened in this session.
- **Do not refresh, upsert, or delete anything.** This skill has no write tools on purpose.

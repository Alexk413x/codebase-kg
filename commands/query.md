---
description: Find code through this repo's committed code graph instead of grepping for it — locate the components with kg_search / kg_node / kg_neighborhood, then read the anchored source to confirm current behavior. Read-only; never edits the graph.
argument-hint: "[what you are looking for]"
---

# /codebase-kg:query

Answer "where does this live?", "what depends on this?" or "what breaks if I change this?" from the
graph, then confirm it in the source.

Run the `kg-query` skill. The division of labor it enforces:

- **The graph is authoritative for where code lives.** It knows the components a search string never
  appears in.
- **The source is authoritative for what the code does now.** The graph is a snapshot taken at build
  time; every behavioral claim traces to a file opened in this session.

This is also the workflow the plugin's search gate hands off to when it denies the first `Grep` of a
session. If the graph turns out not to cover what was asked, say so, run the search, and report the
hole — `/codebase-kg:refresh` is what fills it.

If the repo has no `knowledge/code_graph.db`, say so and offer `/codebase-kg:build`; there is nothing
to query.

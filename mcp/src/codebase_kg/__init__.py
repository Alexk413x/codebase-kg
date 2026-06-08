"""codebase-kg — a local MCP server over a per-repo KNOWLEDGE_GRAPH.md.

Markdown is the source of truth (see ../../SCHEMA.md). `loader` parses it into a
`Graph` of `Node`s; `tools` runs read-only queries over that graph; `server`
exposes those queries as FastMCP tools. `loader` and `tools` depend only on the
standard library, so they are testable without FastMCP installed.
"""

__version__ = "0.1.0"

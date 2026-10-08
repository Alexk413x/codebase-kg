"""Regenerate `src/codebase_kg/catalog.json` and the eval mocks' tool list from the fastmcp registrations.

Run from `mcp/` after changing a tool in `server.py`:

    uv run --frozen python scripts/gen_catalog.py

`test_catalog.py` fails until the committed files match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP / "src"))

from codebase_kg import server

CATALOG = MCP / "src" / "codebase_kg" / "catalog.json"
EVAL_TOOLS = MCP.parent / "evals" / "mocks" / "codebase-kg" / "_tools.json"


def render(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    catalog = server.catalog()
    CATALOG.write_text(render(catalog), encoding="utf-8", newline="\n")
    mocks = json.loads(EVAL_TOOLS.read_text(encoding="utf-8"))
    mocks["tools"] = catalog["tools"]
    EVAL_TOOLS.write_text(render(mocks), encoding="utf-8", newline="\n")
    print(f"wrote {CATALOG} and {EVAL_TOOLS}: {len(catalog['tools'])} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Print Copilot CLI configuration without writing settings or starting a server."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def mcp_config(python: str) -> dict:
    return {"mcpServers": {"codebase-kg": {
        "type": "stdio",
        "command": python,
        "args": ["-I", "-S", str(ROOT / "mcp" / "src" / "codebase_kg" / "shim.py")],
        "env": {},
        "tools": ["*"],
    }}}


def hooks_config(python: str) -> dict:
    return {"version": 1, "hooks": {
        event: [{
            "type": "command", "exec": python,
            "args": [str(ROOT / "hooks" / "kg_copilot.py"), mode],
            "timeoutSec": timeout,
        }]
        for event, mode, timeout in [
            ("preToolUse", "pre", 20), ("postToolUse", "post", 10)
        ]
    }}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=["mcp", "hooks"])
    parser.add_argument("--python", default=sys.executable, help="Python 3.9+ executable on this machine")
    args = parser.parse_args()
    config = mcp_config(args.python) if args.component == "mcp" else hooks_config(args.python)
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main()

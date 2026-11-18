# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Run a codebase-kg CLI from any repo: kg_cli.py build|export|migrate|upgrade|query|server|codex-config|gate [args].

Stdlib only. The CLIs import nothing outside the standard library, so this
puts the plugin's `src` on `sys.path` and runs the module; no venv is built.
"""

import runpy
import sys
from pathlib import Path

COMMANDS = {"build": "build", "export": "export", "migrate": "migrate", "upgrade": "upgrade",
            "query": "query", "server": "control", "codex-config": "codex_config"}

if len(sys.argv) >= 2 and sys.argv[1] == "gate":
    # The search gate lives with the hooks, outside the package: run it by path.
    if sys.argv[2:3] != ["explain"]:
        sys.exit("usage: kg_cli.py gate explain [--tool T] [--path P] [--cwd D] QUERY")
    gate = Path(__file__).resolve().parents[2] / "hooks" / "kg_search_gate.py"
    sys.argv = [str(gate), *sys.argv[2:]]
    runpy.run_path(str(gate), run_name="__main__")
    sys.exit(0)
if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
    sys.exit(f"usage: kg_cli.py {'|'.join([*COMMANDS, 'gate'])} [args]")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module(f"codebase_kg.{COMMANDS[sys.argv.pop(1)]}", run_name="__main__", alter_sys=True)

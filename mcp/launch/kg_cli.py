# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run a codebase-kg CLI from any repo: kg_cli.py build|export|migrate|upgrade [args].

Stdlib only. The CLIs import nothing outside the standard library, so this
puts the plugin's `src` on `sys.path` and runs the module; no venv is built.
"""

import runpy
import sys
from pathlib import Path

COMMANDS = ("build", "export", "migrate", "upgrade")

if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
    sys.exit(f"usage: kg_cli.py {'|'.join(COMMANDS)} [args]")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module(f"codebase_kg.{sys.argv.pop(1)}", run_name="__main__", alter_sys=True)

"""Load `shim.py` by path, without importing the package. Python 3.9 stdlib only."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

SHIM = Path(__file__).resolve().parents[1] / "mcp" / "src" / "codebase_kg" / "shim.py"


def load_shim() -> Any:
    spec = importlib.util.spec_from_file_location("codebase_kg_shim", SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

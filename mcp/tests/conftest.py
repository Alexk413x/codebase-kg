from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make src/ importable even when pytest's pythonpath config isn't applied.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codebase_kg.loader import load_graph  # noqa: E402
from codebase_kg.models import Graph  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def android_graph() -> Graph:
    return load_graph(FIX / "android" / "KNOWLEDGE_GRAPH.md")


@pytest.fixture
def ios_graph() -> Graph:
    return load_graph(FIX / "ios" / "KNOWLEDGE_GRAPH.md")

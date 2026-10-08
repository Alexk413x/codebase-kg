from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Make src/ importable even when pytest's pythonpath config isn't applied.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codebase_kg import migrate
from codebase_kg.models import Anchor, Meta, Node
from codebase_kg.store import CodeGraph
from codebase_kg.writer import build

FIX = Path(__file__).resolve().parent / "fixtures"

# Every server a test starts inherits this, so none takes the real HTTP port 47821.
os.environ["CODEBASE_KG_PORT"] = "0"
os.environ.pop("CLAUDE_PLUGIN_OPTION_SERVER_PORT", None)
for _name in ("CODEBASE_KG_MAX_WORKERS", "CLAUDE_PLUGIN_OPTION_MAX_WORKERS", "CODEBASE_KG_CALL_TIMEOUT"):
    os.environ.pop(_name, None)


@pytest.fixture(scope="session")
def built_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The whole fixtures tree copied to tmp, with each graph migrated in place.

    Copied rather than migrated where it sits because the source files, the
    graph, and the peer graph must keep their relative positions — anchor
    resolution and `counterpart` links are both path-relative — and because
    tests must never write into the repo.
    """
    root = tmp_path_factory.mktemp("fixtures")
    dest = root / "fixtures"
    shutil.copytree(FIX, dest)
    for md in sorted(dest.rglob("KNOWLEDGE_GRAPH.md")):
        migrate.convert(md, md.parent / "code_graph.db")
    return dest


@pytest.fixture
def android_graph(built_fixtures: Path) -> Iterator[CodeGraph]:
    g = CodeGraph(built_fixtures / "android" / "code_graph.db")
    yield g
    g.close()


@pytest.fixture
def ios_graph(built_fixtures: Path) -> Iterator[CodeGraph]:
    g = CodeGraph(built_fixtures / "ios" / "code_graph.db")
    yield g
    g.close()


@pytest.fixture
def wide_graph(built_fixtures: Path) -> Iterator[CodeGraph]:
    g = CodeGraph(built_fixtures / "wide" / "code_graph.db")
    yield g
    g.close()


@pytest.fixture
def sample_meta() -> Meta:
    return Meta(codebase="test", root="src", generated="2026-07-30", language="kotlin")


@pytest.fixture
def sample_nodes() -> list[Node]:
    return [
        Node(
            id="feed_ranker",
            kind="Domain (pure)",
            description="Ranks the feed by freshness and per-source weight.",
            anchors=[Anchor("domain/FeedRanker.kt", "FeedRanker")],
            edges=["saved_article"],
            section="DOMAIN",
        ),
        Node(
            id="saved_article",
            kind="Room @Entity",
            description="Persisted bookmark in the saved_articles table.",
            anchors=[Anchor("room/SavedArticleEntity.kt", "SavedArticleEntity")],
            edges=[],
            section="DOMAIN",
        ),
    ]


@pytest.fixture
def sample_db(tmp_path: Path, sample_meta: Meta, sample_nodes: list[Node]) -> Path:
    db = tmp_path / "code_graph.db"
    build(db, sample_meta, sample_nodes)
    return db

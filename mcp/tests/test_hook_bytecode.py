"""The vendored hooks must not leave bytecode in the repo they run in.

`kg_pre_commit.py` imports `kg_pre_push.py` from the hooks dir, so a default
interpreter writes `.githooks/__pycache__/kg_pre_push.cpython-*.pyc` into the
host repo, where it shows up as an untracked file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GIT_HOOKS = Path(__file__).resolve().parent.parent.parent / "git-hooks"


@pytest.mark.parametrize("script", ["kg_pre_commit.py", "kg_pre_push.py"])
def test_checker_leaves_no_pycache(tmp_path: Path, script: str) -> None:
    repo = tmp_path / "repo"
    hooks = repo / ".githooks"
    hooks.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("kg_pre_commit.py", "kg_pre_push.py"):
        shutil.copy2(GIT_HOOKS / name, hooks / name)

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    subprocess.run(
        [sys.executable, str(hooks / script)],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert not (hooks / "__pycache__").exists(), sorted(p.name for p in (hooks / "__pycache__").iterdir())
    untracked = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert not [ln for ln in untracked if ln.endswith(".pyc")], untracked

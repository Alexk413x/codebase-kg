"""Keep the codebase-kg paths in Codex's `config.toml` on the installed plugin. Stdlib only.

`kg_cli.py codex-config` prints a table whose path names the plugin folder, and
Codex installs each plugin version in its own folder. After an update the table
names the old folder. The Codex SessionStart hook calls `refresh`, which moves
these values to the plugin that runs the hook:

- `http_headers_helper` and `command` in `[mcp_servers.codebase-kg]`
- `CLAUDE_PLUGIN_ROOT` in `[shell_environment_policy.set]`, when it names a
  codebase-kg folder

A value moves only when its folder is missing or holds an older release, so a
table that names a development checkout stays as written. Every other line of
the file is written back byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

SERVER_TABLE = "mcp_servers.codebase-kg"
ENV_TABLE = "shell_environment_policy.set"
LAUNCHERS = ("kg_headers.py", "kg-shim", "kg-shim.cmd")

_TABLE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_ASSIGN = re.compile(
    r"^(\s*[A-Za-z0-9_-]+\s*=\s*)('[^']*'|\"(?:[^\"\\]|\\.)*\")(\s*(?:#.*)?)$"
)
_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)")
_WORD = re.compile(r'"[^"]*"|\S+')


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = (env.get("CODEX_HOME") or "").strip()
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def version_of(root: Path) -> str:
    for manifest in (".codex-plugin", ".claude-plugin"):
        try:
            data = json.loads((root / manifest / "plugin.json").read_text(encoding="utf-8"))
            if data.get("name") == "codebase-kg" and data.get("version"):
                return str(data["version"])
        except (OSError, ValueError, AttributeError):
            continue
    return ""


def superseded(old_root: Path, plugin_root: Path) -> bool:
    """True when `old_root` is missing or holds a release older than `plugin_root`."""
    if os.path.normcase(str(old_root)) == os.path.normcase(str(plugin_root)):
        return False
    if not old_root.is_dir():
        return True
    old, new = version_of(old_root), version_of(plugin_root)
    return bool(old and new) and _version(old) < _version(new)


def _decode(literal: str) -> str | None:
    """The string a TOML literal holds, or None for an escape this file does not write."""
    body = literal[1:-1]
    if literal[0] == "'":
        return body
    if re.search(r'\\[^\\"]', body.replace("\\\\", "")):
        return None
    return body.replace('\\"', '"').replace("\\\\", "\\")


def _encode(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _moved_launcher(path: str, plugin_root: Path) -> str | None:
    """`path` under `plugin_root`, when it is a codebase-kg launcher in a superseded folder."""
    old = Path(path)
    if old.name not in LAUNCHERS or old.parent.name != "launch" or old.parent.parent.name != "mcp":
        return None
    if not superseded(old.parents[2], plugin_root):
        return None
    return str(plugin_root / "mcp" / "launch" / old.name)


def _moved_value(table: str, key: str, value: str, plugin_root: Path) -> str | None:
    if table == SERVER_TABLE and key == "command":
        return _moved_launcher(value, plugin_root)
    if table == SERVER_TABLE and key == "http_headers_helper":
        for word in _WORD.finditer(value):
            new = _moved_launcher(word.group().strip('"'), plugin_root)
            if new:
                quoted = f'"{new}"' if " " in new else new
                return value[: word.start()] + quoted + value[word.end() :]
        return None
    ours = table == ENV_TABLE and key == "CLAUDE_PLUGIN_ROOT" and "codebase-kg" in value.lower()
    if ours and superseded(Path(value), plugin_root):
        return str(plugin_root)
    return None


def rewritten(text: str, plugin_root: Path) -> str | None:
    """`text` with the superseded codebase-kg paths moved to `plugin_root`, or None when nothing moves."""
    table = ""
    changed = False
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n").lstrip("﻿")
        header = _TABLE.match(body)
        if header:
            table = re.sub(r"[\s\"']", "", header.group(1))
            continue
        if table not in (SERVER_TABLE, ENV_TABLE):
            continue
        assign = _ASSIGN.match(body)
        key = _KEY.match(body)
        if not assign or not key:
            continue
        value = _decode(assign.group(2))
        if value is None:
            continue
        new = _moved_value(table, key.group(1), value, plugin_root)
        if new is None or new == value:
            continue
        lines[i] = line.replace(body, assign.group(1) + _encode(new) + assign.group(3))
        changed = True
    return "".join(lines) if changed else None


def refresh(plugin_root: Path, env: Mapping[str, str] | None = None) -> str | None:
    """Move the paths in Codex's config to `plugin_root`. Returns the line to print, or None."""
    path = config_path(env)
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    new = rewritten(text, plugin_root)
    if new is None:
        return None
    # A symlinked config.toml (a dotfiles repo) stays a link: the real file is replaced.
    target = Path(os.path.realpath(path))
    tmp = target.with_name(target.name + ".codebase-kg.tmp")
    try:
        tmp.write_bytes(new.encode("utf-8"))
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    version = version_of(plugin_root)
    return (
        f"codebase-kg: moved the codebase-kg paths in {path} to the installed plugin"
        f"{' ' + version if version else ''}. If the codebase-kg tools are missing in this "
        "session, restart Codex."
    )

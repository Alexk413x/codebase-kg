"""Every registered tool is reachable, and every named tool is registered.

A structure review found `kg_neighborhood` and `kg_find_by_link` registered on
the server, covered by tests, documented in the README — and named in no skill's
`allowed-tools`, so nothing could call them. The gate's own denial message
instructed an agent to use `kg_neighborhood` while every skill it might then
invoke was forbidden from doing so. Dead surface is invisible to unit tests by
construction: each half is fine, only the join is broken.

The same review found the skills allowlisting only `mcp__codebase-kg__*` though
the plugin's own hook documents that the host also names the server
`mcp__plugin_codebase-kg_codebase-kg__*` under plugin load.

These tests are about the plugin as an assembled thing, so they read the shipped
markdown rather than the Python.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - yaml ships with the dev env
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS = sorted(ROOT.joinpath("skills").glob("*/SKILL.md"))
COMMANDS = sorted(ROOT.joinpath("commands").glob("*.md"))
SERVER = ROOT / "mcp" / "src" / "codebase_kg" / "server.py"

BARE = "mcp__codebase-kg__"
PLUGIN = "mcp__plugin_codebase-kg_codebase-kg__"


def registered_tools() -> set[str]:
    """The tool names the MCP server actually exposes."""
    src = SERVER.read_text(encoding="utf-8")
    return set(re.findall(r"^def (kg_\w+)", src, re.M))


def frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path.name} has no frontmatter"
    assert yaml is not None, "pyyaml is required for the surface tests"
    data = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(data, dict), f"{path.name} frontmatter is not a mapping"
    return data


def allowed_entries(path: Path) -> list[str]:
    """`allowed-tools` as a list of strings, whatever YAML made of it."""
    raw = frontmatter(path).get("allowed-tools") or []
    assert isinstance(raw, list), f"{path.name}: allowed-tools is not a list"
    return [str(entry) for entry in raw]


def allowed_kg_tools(path: Path) -> set[str]:
    """The bare `kg_*` names a skill permits, both prefixes folded together."""
    out: set[str] = set()
    for entry in allowed_entries(path):
        for prefix in (PLUGIN, BARE):  # PLUGIN first — it contains BARE's suffix
            if entry.startswith(prefix):
                out.add(entry[len(prefix):])
                break
    return out


def test_there_are_skills_to_check() -> None:
    """A glob that silently matches nothing would make every test below pass."""
    assert len(SKILLS) >= 6, [p.parent.name for p in SKILLS]
    assert len(COMMANDS) >= 6, [p.name for p in COMMANDS]


# --- nothing invented --------------------------------------------------------
@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_every_tool_a_skill_allows_is_registered(skill: Path) -> None:
    unknown = allowed_kg_tools(skill) - registered_tools()
    assert not unknown, f"{skill.parent.name} allows tools the server does not expose: {unknown}"


# `kg_`-shaped names in the docs that are deliberately not MCP tools: the
# vendored git hooks, the in-session hooks, and a config key.
NOT_TOOLS = {
    "kg_pre_commit", "kg_pre_push", "kg_post_edit_check", "kg_search_gate", "kg_path",
}


def named_tools(text: str) -> set[str]:
    """The `kg_*` tool names a document refers to.

    The MCP prefixes are stripped first, or a plain `\\bkg_\\w+\\b` scan reads
    `mcp__plugin_codebase-kg_codebase-kg__kg_stats` as the two fragments
    `kg_codebase` and `kg__kg_stats` and every document fails.
    """
    text = text.replace(PLUGIN, " ").replace(BARE, " ")
    return set(re.findall(r"(?<![\w-])(kg_[a-z_]+)\b", text)) - NOT_TOOLS


@pytest.mark.parametrize(
    "doc", SKILLS + COMMANDS, ids=lambda p: p.parent.name if p.name == "SKILL.md" else p.stem
)
def test_every_tool_a_document_names_in_prose_is_registered(doc: Path) -> None:
    """Catches a renamed tool that only survives in an instruction."""
    unknown = named_tools(doc.read_text(encoding="utf-8")) - registered_tools()
    assert not unknown, f"{doc} names tools the server does not expose: {sorted(unknown)}"


# --- nothing stranded --------------------------------------------------------
def test_every_registered_tool_is_reachable_from_some_skill() -> None:
    """The dead-surface check. A tool no skill may call is capability nobody can
    reach, however well it is implemented and tested."""
    reachable: set[str] = set()
    for skill in SKILLS:
        reachable |= allowed_kg_tools(skill)
    stranded = registered_tools() - reachable
    assert not stranded, f"registered but reachable from no skill: {sorted(stranded)}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_a_skill_allows_both_names_the_host_may_give_the_server(skill: Path) -> None:
    """`hooks/kg_search_gate.py` matches both forms and `hooks.json` matches both
    forms; a skill that lists only one is denied its own tools under the other."""
    tools = allowed_entries(skill)
    bare = {t[len(BARE):] for t in tools if t.startswith(BARE)}
    plugin = {t[len(PLUGIN):] for t in tools if t.startswith(PLUGIN)}
    assert bare == plugin, (
        f"{skill.parent.name} lists different tools under the two server names; "
        f"only bare: {sorted(bare - plugin)}, only plugin: {sorted(plugin - bare)}"
    )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_a_skill_that_must_ask_for_the_cli_may_call_kg_stats(skill: Path) -> None:
    """The CLI invocation is only knowable from `kg_stats`'s `cli` field. A skill
    told to read it, but not permitted to call it, is back to guessing a path it
    cannot see."""
    text = skill.read_text(encoding="utf-8")
    if "read its `cli` field" not in text:
        pytest.skip("this skill runs no CLI")
    assert "kg_stats" in allowed_kg_tools(skill), (
        f"{skill.parent.name} is told to read kg_stats's cli field but may not call kg_stats"
    )


# --- the gate hands off to something that exists -----------------------------
def test_the_gate_message_names_only_real_tools() -> None:
    """The gate denies a search and tells the agent what to run instead. If that
    instruction names a tool the server does not expose, the gate has stranded
    the agent it interrupted."""
    import sys

    sys.path.insert(0, str(ROOT / "hooks"))
    import kg_search_gate as gate

    named = named_tools(gate.GATE_MESSAGE)
    assert named, "the gate message no longer names a tool to run"
    unknown = named - registered_tools()
    assert not unknown, f"gate message names unknown tools: {sorted(unknown)}"


def test_the_gate_hands_off_to_a_skill_that_exists() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "hooks"))
    import kg_search_gate as gate

    referenced = re.findall(r"/codebase-kg:(\w+)", gate.GATE_MESSAGE)
    for name in referenced:
        assert (ROOT / "commands" / f"{name}.md").is_file(), f"gate points at missing /{name}"


# --- commands and skills line up ---------------------------------------------
def test_every_command_that_delegates_names_a_real_skill() -> None:
    skill_names = {p.parent.name for p in SKILLS}
    for cmd in COMMANDS:
        for name in re.findall(r"`(kg-[a-z]+)`\s+skill|Run the `(kg-[a-z]+)`", cmd.read_text(encoding="utf-8")):
            named = next(filter(None, name), None)
            if named:
                assert named in skill_names, f"{cmd.name} delegates to missing skill {named}"

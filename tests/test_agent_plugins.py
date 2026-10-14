"""The Claude Code and Codex plugin packaging must be installable as written."""

import json
import re
from pathlib import Path

import pytest

from agent.teams import parse_team_spec

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "multi-agents"
SKILL = PLUGIN / "skills" / "multi-agents" / "SKILL.md"
COMMAND = PLUGIN / "commands" / "team.md"

CLAUDE_MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
CODEX_MANIFEST = PLUGIN / "plugin.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(path: Path) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def plugin_source(entry: dict) -> str:
    source = entry["source"]
    return source if isinstance(source, str) else source["path"]


@pytest.mark.parametrize("marketplace", [CLAUDE_MARKETPLACE, CODEX_MARKETPLACE], ids=["claude", "codex"])
def test_marketplace_lists_the_plugin_at_an_existing_relative_path(marketplace):
    data = load(marketplace)
    assert data["name"]
    entries = {entry["name"]: entry for entry in data["plugins"]}
    assert list(entries) == ["multi-agents"]
    path = plugin_source(entries["multi-agents"])
    assert path.startswith("./") and ".." not in Path(path).parts
    assert (ROOT / path).resolve() == PLUGIN.resolve()


def test_codex_marketplace_entry_declares_install_policy_and_category():
    (entry,) = load(CODEX_MARKETPLACE)["plugins"]
    assert entry["policy"]["installation"] in {"AVAILABLE", "INSTALLED_BY_DEFAULT"}
    assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
    assert entry["category"]


def test_both_manifests_name_and_version_the_same_plugin():
    claude, codex = load(CLAUDE_MANIFEST), load(CODEX_MANIFEST)
    assert claude["name"] == codex["name"] == "multi-agents"
    (claude_entry,) = load(CLAUDE_MARKETPLACE)["plugins"]
    assert claude["version"] == codex["version"] == claude_entry["version"]
    assert claude["description"] and codex["description"]


def test_manifest_paths_exist():
    # Neither manifest overrides the default component locations, so the defaults must exist.
    assert SKILL.is_file()
    assert COMMAND.is_file()
    assert not (PLUGIN / ".claude-plugin" / "skills").exists()
    for manifest in (load(CLAUDE_MANIFEST), load(CODEX_MANIFEST)):
        for key in ("skills", "commands", "agents", "hooks"):
            if key in manifest:
                targets = manifest[key] if isinstance(manifest[key], list) else [manifest[key]]
                for target in targets:
                    assert (PLUGIN / target).exists(), f"{key} -> {target}"


def test_skill_and_command_have_the_frontmatter_both_clis_require():
    skill = frontmatter(SKILL)
    assert skill["name"] == SKILL.parent.name
    assert skill["description"]
    assert frontmatter(COMMAND)["description"]


def test_command_passes_the_user_text_through():
    assert "$ARGUMENTS" in COMMAND.read_text(encoding="utf-8")


def test_specs_shown_in_the_skill_are_valid():
    text = SKILL.read_text(encoding="utf-8")
    specs = {
        span
        for span in re.findall(r"`([a-z]+:[^`]+)`", text)
        if span.split(":")[0] in {"solo", "review", "best"} and not re.search(r"[<\[]|\bH\b", span)
    }
    assert specs, "the skill should show at least one concrete spec"
    for spec in specs:
        parse_team_spec(spec)


def test_skill_names_the_published_cli_command():
    text = SKILL.read_text(encoding="utf-8")
    assert "uvx --from git+https://github.com/BichengWang/multi-agents multi-agents run" in text

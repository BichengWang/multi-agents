"""The Claude Code and Codex plugin packaging must be installable as written."""

import json
import re
from pathlib import Path

import pytest

from agent.teams import build_team

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
    found = re.findall(r"(?<![\w-])[a-z]+:[a-z_]+=[^\s`'\"]+", text)
    # Templates such as `solo:worker=H` or `...<spec>` document the grammar, not a usable spec.
    specs = {spec.rstrip(".,;:)") for spec in found if not re.search(r"[<\[]|=H\b|[,+]H\b", spec)}
    assert {"review:coder=codex@gpt-6-luna,reviewer=claude@haiku,rounds=2"} <= specs
    assert any("reviewer=check" in spec for spec in specs)
    for spec in specs:
        build_team(spec, check="true")


def test_command_allows_the_cli_invocation_the_skill_runs():
    # A mismatch makes Claude Code ask for (or deny) the exact command the skill tells it to run.
    allowed = re.search(r"Bash\((uvx [^:]+):\*\)", frontmatter(COMMAND)["allowed-tools"])
    assert allowed, "the command does not allow a uvx invocation"
    assert f"{allowed.group(1)} \\\n" in SKILL.read_text(encoding="utf-8")

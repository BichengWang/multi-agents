import pytest

from agent.teams import SpecError, build_harness, build_team, is_priced, parse_team_spec

VALID = [
    ("solo:worker=claude", "solo:worker=claude"),
    ("solo:worker=codex@gpt-6-luna", "solo:worker=codex@gpt-6-luna"),
    ("  SOLO:worker=Claude@haiku ", "solo:worker=claude@haiku"),
    ("review:coder=codex,reviewer=claude@haiku", "review:coder=codex,reviewer=claude@haiku,rounds=3"),
    ("review:reviewer=claude,coder=codex,rounds=2", "review:coder=codex,reviewer=claude,rounds=2"),
    ("best:workers=claude@haiku+codex", "best:workers=claude@haiku+codex,judge=claude@haiku"),
    ("best:workers=codex+claude+sdk@gpt-5-mini,judge=sdk", "best:workers=codex+claude+sdk@gpt-5-mini,judge=sdk"),
    (
        "solo:worker=sdk@litellm/openrouter/anthropic/claude-3.5-sonnet:beta",
        "solo:worker=sdk@litellm/openrouter/anthropic/claude-3.5-sonnet:beta",
    ),
]


@pytest.mark.parametrize("spec,canonical", VALID)
def test_valid_specs_have_an_idempotent_canonical_name(spec, canonical):
    team = build_team(spec)
    assert team.name == canonical
    assert build_team(team.name).name == canonical


def test_model_ids_keep_their_separators():
    harness = build_harness("sdk@litellm/openrouter/anthropic/claude-3.5-sonnet:beta")
    assert harness.model == "litellm/openrouter/anthropic/claude-3.5-sonnet:beta"


INVALID = [
    "claude",
    "duo:worker=claude",
    "solo:worker=gemini",
    "solo:worker=claude@",
    "solo:worker=claude,extra=1",
    "solo:",
    "solo:worker",
    "solo:worker=claude,worker=codex",
    "review:coder=codex",
    "review:coder=codex,reviewer=claude,rounds=0",
    "review:coder=codex,reviewer=claude,rounds=x",
    "best:workers=claude",
    "best:workers=claude+,judge=claude",
    "best:judge=claude",
]


@pytest.mark.parametrize("spec", INVALID)
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(SpecError):
        parse_team_spec(spec)
    with pytest.raises(SpecError):
        build_team(spec)


def test_is_priced_follows_the_price_table():
    assert is_priced("claude@haiku")
    assert is_priced("codex@gpt-6-luna")
    assert not is_priced("codex")  # effective model unknown until run time
    assert not is_priced("codex@some-unlisted-model")
    assert is_priced("sdk@gpt-5-mini-2025-08-07")

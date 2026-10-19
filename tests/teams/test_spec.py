import pytest

from agent.teams import SpecError, build_harness, build_team, is_priced, parse_team_spec
from agent.teams.plan import PlanTeam

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
    (
        "plan:planner=claude,workers=claude+codex",
        "plan:planner=claude,workers=claude+codex,integrator=claude,max_tasks=4,replans=1",
    ),
    (
        "plan:replans=0,workers=codex,planner=claude@haiku,max_tasks=2,integrator=sdk",
        "plan:planner=claude@haiku,workers=codex,integrator=sdk,max_tasks=2,replans=0",
    ),
    (
        "plan:planner=claude,workers=claude+codex,reviewer=codex@gpt-6-luna",
        "plan:planner=claude,workers=claude+codex,integrator=claude,max_tasks=4,replans=1,"
        "reviewer=codex@gpt-6-luna,rounds=3",
    ),
    (
        "plan:rounds=2,reviewer=claude,planner=claude,workers=codex,replans=3",
        "plan:planner=claude,workers=codex,integrator=codex,max_tasks=4,replans=3,reviewer=claude,rounds=2",
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
    "plan:planner=claude,workers=claude,replans=-1",
    "plan:planner=claude,workers=claude,replans=x",
    "plan:planner=claude,workers=claude,rounds=2",
    "plan:planner=claude,workers=claude,reviewer=claude,rounds=0",
    "plan:planner=claude,workers=claude,reviewer=claude,rounds=x",
    "plan:planner=claude,workers=claude,reviewer=check",
    "plan:planner=claude,workers=claude,reviewer=gemini",
]


@pytest.mark.parametrize("spec", INVALID)
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(SpecError):
        parse_team_spec(spec)
    with pytest.raises(SpecError):
        build_team(spec)


def test_plan_spec_fields_and_harness_specs_include_the_reviewer():
    spec = parse_team_spec("plan:planner=claude,workers=codex+sdk@gpt-5-mini,reviewer=claude@haiku,rounds=2,replans=0")
    assert (spec.replans, spec.reviewer.model, spec.rounds) == (0, "haiku", 2)
    assert [str(h) for h in spec.harness_specs] == ["claude@haiku", "claude", "codex", "codex", "sdk@gpt-5-mini"]
    team = build_team(spec.canonical)
    assert isinstance(team, PlanTeam)
    assert (team.replans, team.rounds, team.reviewer.model) == (0, 2, "haiku")
    assert parse_team_spec("plan:planner=claude,workers=codex").reviewer is None


def test_a_plan_reviewer_that_is_the_check_command_is_explained():
    with pytest.raises(SpecError, match="integrated patch"):
        build_team("plan:planner=claude,workers=claude,reviewer=check", check="true")


def test_rounds_without_a_reviewer_is_explained():
    with pytest.raises(SpecError, match="only valid together with reviewer"):
        parse_team_spec("plan:planner=claude,workers=claude,rounds=2")


def test_is_priced_follows_the_price_table():
    assert is_priced("claude@haiku")
    assert is_priced("codex@gpt-6-luna")
    assert not is_priced("codex")  # effective model unknown until run time
    assert not is_priced("codex@some-unlisted-model")
    assert is_priced("sdk@gpt-5-mini-2025-08-07")

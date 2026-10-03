import asyncio

import pytest

pytest.importorskip("agents")

from agent.story_agent_simple.manager import SimpleStoryManager  # noqa: E402
from agent.story_agent_simple.my_agents.evaluator_agent import StoryEvaluation  # noqa: E402
from agent.workflows import CheckpointDecision  # noqa: E402

from .fakes import FakeAgent, counting_generator, fake_runner  # noqa: E402


def story_eval(score: float) -> StoryEvaluation:
    return StoryEvaluation(
        score=score, passed=score >= 8, feedback="tighten act two",
        plot_coherence=score, character_development=score, originality=score,
        emotional_impact=score, market_appeal=score,
    )


def make_manager(mode, scores, **kw):
    it = iter(scores)
    evaluator = FakeAgent("StoryEvaluator", lambda _: story_eval(next(it)))
    return SimpleStoryManager(
        mode=mode, runner=fake_runner, generator=counting_generator("story"), evaluator=evaluator, **kw
    )


@pytest.mark.parametrize(
    "mode,scores,expected",
    [
        ("single", [6], "story-1"),
        ("refine", [6, 9], "story-2"),
        ("best-of-n", [5, 9, 7], None),
    ],
)
def test_modes(mode, scores, expected, capsys):
    result = asyncio.run(make_manager(mode, scores).run("a heist on the moon"))
    out = capsys.readouterr().out
    assert "Generated Story:" in out and "plot_coherence" in out
    if expected:
        assert expected in out
    if mode == "best-of-n":
        assert result.verdict.score == 9


def test_real_agents_have_structured_evaluator():
    from agent.story_agent_simple.my_agents.evaluator_agent import evaluator_agent

    assert evaluator_agent.output_type is StoryEvaluation


def test_unknown_mode():
    with pytest.raises(ValueError):
        SimpleStoryManager(mode="nope").build_workflow()


def test_manager_forwards_checkpoint_to_refine_loop():
    async def checkpoint(_state):
        return CheckpointDecision(continue_refining=False)

    result = asyncio.run(make_manager("refine", [6, 9], checkpoint=checkpoint).run("idea"))
    assert result.final_output == "story-1"
    assert result.meta["checkpoint_stopped"] is True


def test_story_cli_rejects_checkpoint_outside_refine_mode(monkeypatch, capsys):
    from agent.story_agent_simple.main import main

    monkeypatch.setattr("sys.argv", ["story", "idea", "--mode", "single", "--checkpoint"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main())
    assert exc.value.code == 2
    assert "--checkpoint requires --mode refine" in capsys.readouterr().err

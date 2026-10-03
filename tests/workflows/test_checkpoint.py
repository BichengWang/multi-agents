import asyncio

import pytest

from agent.workflows import (
    CheckpointDecision,
    RefineCheckpoint,
    RefineLoop,
    Verdict,
    cli_checkpoint,
    to_jsonable,
)

from .fakes import counting_generator, fake_runner, scripted_evaluator


def test_checkpoint_steers_revision_and_records_decision():
    states = []

    async def checkpoint(state):
        states.append(state)
        return CheckpointDecision(feedback="Give the protagonist a clear motive.")

    generator = counting_generator()
    result = asyncio.run(
        RefineLoop(
            generator, scripted_evaluator([5, 9]), runner=fake_runner, checkpoint=checkpoint,
        ).run("a moon heist")
    )

    assert len(states) == 1
    assert states[0].query == "a moon heist"
    assert states[0].iteration == 1
    assert states[0].draft == "draft-1"
    assert states[0].verdict.score == 5
    assert "feedback for 5" in generator.calls[1]
    assert "Human steering feedback:\nGive the protagonist a clear motive." in generator.calls[1]
    assert result.final_output == "draft-2"
    assert result.meta["checkpoint_stopped"] is False
    checkpoint_step = next(step for step in result.steps if step.name == "checkpoint")
    assert checkpoint_step.iteration == 1
    assert to_jsonable(checkpoint_step.output) == {
        "continue_refining": True, "feedback": "Give the protagonist a clear motive.",
    }


def test_checkpoint_stop_returns_best_draft_without_another_call():
    async def checkpoint(state):
        return CheckpointDecision(continue_refining=state.iteration < 2)

    generator = counting_generator()
    result = asyncio.run(
        RefineLoop(
            generator, scripted_evaluator([7, 4, 9]), max_iterations=4,
            runner=fake_runner, checkpoint=checkpoint,
        ).run("idea")
    )

    assert len(generator.calls) == 2
    assert result.final_output == "draft-1"
    assert result.verdict.score == 7
    assert result.meta == {
        "iterations": 2, "accepted": False, "scores": [7, 4], "checkpoint_stopped": True,
    }


@pytest.mark.parametrize("score,max_iterations,threshold", [(9, 3, None), (3, 1, None), (6, 3, 6)])
def test_checkpoint_is_skipped_when_no_revision_is_needed(score, max_iterations, threshold):
    async def checkpoint(_state):
        pytest.fail("A terminal draft must not prompt for another revision")

    result = asyncio.run(
        RefineLoop(
            counting_generator(), scripted_evaluator([score]), max_iterations=max_iterations,
            threshold=threshold, runner=fake_runner, checkpoint=checkpoint,
        ).run("idea")
    )
    assert result.meta["iterations"] == 1
    assert result.meta["checkpoint_stopped"] is False
    assert [step.name for step in result.steps] == ["generate", "evaluate"]


def test_checkpoint_preserves_custom_revision_prompt():
    async def checkpoint(_state):
        return CheckpointDecision(feedback="Focus on the ending.")

    generator = counting_generator()
    asyncio.run(
        RefineLoop(
            generator, scripted_evaluator([5, 9]), runner=fake_runner, checkpoint=checkpoint,
            revision_prompt=lambda query, draft, verdict: f"custom: {query}/{draft}/{verdict.score}",
        ).run("idea")
    )
    assert generator.calls[1] == (
        "custom: idea/draft-1/5.0\n\nHuman steering feedback:\nFocus on the ending."
    )


def test_checkpoint_rejects_invalid_decision_before_revising():
    async def checkpoint(_state):
        return None

    generator = counting_generator()
    with pytest.raises(TypeError, match="CheckpointDecision"):
        asyncio.run(
            RefineLoop(
                generator, scripted_evaluator([5]), runner=fake_runner, checkpoint=checkpoint,
            ).run("idea")
        )
    assert len(generator.calls) == 1


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("", CheckpointDecision()),
        ("  strengthen the ending  ", CheckpointDecision(feedback="strengthen the ending")),
        (" /STOP ", CheckpointDecision(continue_refining=False)),
    ],
)
def test_cli_checkpoint_displays_draft_and_handles_response(monkeypatch, capsys, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _: answer)
    state = RefineCheckpoint("idea", 2, "my draft", Verdict(score=5, passed=False, feedback="revise"))
    assert asyncio.run(cli_checkpoint(state)) == expected
    output = capsys.readouterr().out
    assert "Draft after iteration 2:\nmy draft" in output
    assert "Reviewer score: 5.0/10\nFeedback:\nrevise" in output


def test_cli_checkpoint_stops_on_closed_input(monkeypatch):
    def closed_input(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    state = RefineCheckpoint("idea", 1, "draft", Verdict(score=5, passed=False, feedback="revise"))
    assert asyncio.run(cli_checkpoint(state)) == CheckpointDecision(continue_refining=False)

"""Offline tests for agent.core patterns, with run_agent replaced by a scripted fake."""

import asyncio
from types import SimpleNamespace

import pytest
from agents import Agent

from agent.core import Evaluation, fan_out, pipeline, refine_loop
from agent.core import runner

generator = Agent(name="gen", instructions="write")
evaluator = Agent(name="eval", instructions="judge", output_type=Evaluation)


def _result(output):
    return SimpleNamespace(final_output=output, final_output_as=lambda _cls: output)


def _evaluation(score, passed):
    return Evaluation(score=score, passed=passed, strengths=[], issues=[f"issue@{score}"], suggestions="fix it")


@pytest.fixture
def fake_runs(monkeypatch):
    """Script agent outputs per agent name and record every input the fake receives."""
    scripts: dict[str, list] = {}
    calls: list[tuple[str, str]] = []

    async def fake_run_agent(agent, input, **_kwargs):
        calls.append((agent.name, input))
        output = scripts[agent.name].pop(0)
        if isinstance(output, Exception):
            raise output
        return _result(output)

    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    return scripts, calls


def test_refine_loop_stops_when_passed(fake_runs):
    scripts, calls = fake_runs
    scripts["gen"] = ["draft1", "draft2", "draft3"]
    scripts["eval"] = [_evaluation(5, False), _evaluation(9, True), _evaluation(10, True)]

    result = asyncio.run(refine_loop(generator, evaluator, "a story", max_rounds=3))

    assert result.output == "draft2"
    assert len(result.rounds) == 2
    # The revision prompt carries the previous draft and the evaluator's feedback.
    revision_input = [inp for name, inp in calls if name == "gen"][1]
    assert "draft1" in revision_input and "issue@5" in revision_input and "fix it" in revision_input


def test_refine_loop_returns_best_draft_when_budget_runs_out(fake_runs):
    scripts, _ = fake_runs
    scripts["gen"] = ["draft1", "draft2"]
    scripts["eval"] = [_evaluation(7, False), _evaluation(4, False)]

    result = asyncio.run(refine_loop(generator, evaluator, "a story", max_rounds=2))

    assert result.output == "draft1"
    assert result.evaluation.score == 7
    assert len(result.rounds) == 2


def test_refine_loop_requires_threshold_even_if_passed(fake_runs):
    scripts, _ = fake_runs
    scripts["gen"] = ["draft1", "draft2"]
    scripts["eval"] = [_evaluation(6, True), _evaluation(9, True)]

    result = asyncio.run(refine_loop(generator, evaluator, "a story", max_rounds=2, threshold=8))

    assert result.output == "draft2"


def test_pipeline_chains_outputs(fake_runs):
    scripts, calls = fake_runs
    scripts["gen"] = ["step1"]
    scripts["eval"] = ["step2"]

    outputs = asyncio.run(pipeline([generator, evaluator], "start"))

    assert outputs == ["step1", "step2"]
    assert calls == [("gen", "start"), ("eval", "step1")]


def test_fan_out_preserves_order_and_tolerates_failures(fake_runs):
    scripts, _ = fake_runs
    scripts["gen"] = ["a", RuntimeError("boom"), "c"]

    outputs = asyncio.run(fan_out(generator, ["1", "2", "3"], concurrency=1))

    assert outputs == ["a", None, "c"]


def test_run_agent_retries_then_succeeds(monkeypatch):
    attempts = []

    async def flaky_run(agent, input, **_kwargs):
        attempts.append(input)
        if len(attempts) < 2:
            raise RuntimeError("transient")
        return _result("ok")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(runner.Runner, "run", flaky_run)
    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)

    result = asyncio.run(runner.run_agent(generator, "hi", max_retries=2))

    assert result.final_output == "ok"
    assert len(attempts) == 2


def test_evaluation_score_bounds():
    with pytest.raises(ValueError):
        Evaluation(score=11, passed=True, strengths=[], issues=[], suggestions="")

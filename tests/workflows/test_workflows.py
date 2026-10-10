import asyncio

import pytest

from agent.workflows import BestOfN, RefineLoop, RunTracker, SequentialWorkflow, Step, Verdict

from .fakes import ConcurrentFakeRunner, FakeAgent, counting_generator, fake_runner, scripted_evaluator


def run(coro):
    return asyncio.run(coro)


def test_sequential_pipes_outputs_and_records_steps():
    upper = FakeAgent("Upper", str.upper)
    exclaim = FakeAgent("Exclaim", lambda s: s + "!")
    wf = SequentialWorkflow(
        [Step("upper", upper), Step("exclaim", exclaim, prepare=lambda q, prev: f"{prev} ({q})")],
        runner=fake_runner,
    )
    result = run(wf.run("hi"))
    assert result.final_output == "HI (hi)!"
    assert [s.name for s in result.steps] == ["upper", "exclaim"]
    assert exclaim.calls == ["HI (hi)"]


def test_refine_stops_when_evaluator_passes():
    gen = counting_generator()
    ev = scripted_evaluator([5, 7, 9, 10])
    result = run(RefineLoop(gen, ev, max_iterations=5, runner=fake_runner).run("idea"))
    assert result.final_output == "draft-3"
    assert result.meta == {"iterations": 3, "accepted": True, "scores": [5, 7, 9]}
    # Revisions carry the original request, the previous draft, and the feedback.
    assert gen.calls[0] == "idea"
    assert "idea" in gen.calls[1] and "draft-1" in gen.calls[1] and "feedback for 5" in gen.calls[1]


def test_refine_threshold_overrides_passed_flag():
    gen = counting_generator()
    ev = scripted_evaluator([6, 7, 9], pass_at=100)  # evaluator never sets passed
    result = run(RefineLoop(gen, ev, max_iterations=5, threshold=7, runner=fake_runner).run("idea"))
    assert result.meta["iterations"] == 2
    assert result.meta["accepted"] is True


def test_refine_returns_best_draft_when_never_accepted():
    gen = counting_generator()
    ev = scripted_evaluator([4, 7, 5])
    result = run(RefineLoop(gen, ev, max_iterations=3, runner=fake_runner).run("idea"))
    assert result.final_output == "draft-2"
    assert result.verdict.score == 7
    assert result.meta["accepted"] is False
    assert len(result.outputs("generate")) == 3


def test_refine_rejects_unstructured_evaluator():
    ev = FakeAgent("PlainEvaluator", lambda _: "looks great")
    with pytest.raises(TypeError, match="score/passed/feedback"):
        run(RefineLoop(counting_generator(), ev, runner=fake_runner).run("idea"))


def test_refine_validates_max_iterations():
    with pytest.raises(ValueError):
        RefineLoop(counting_generator(), scripted_evaluator([1]), max_iterations=0)


def test_best_of_n_picks_highest_score_with_variants():
    gen = FakeAgent("Gen", lambda text: text)
    scores = {"q#1": 6.0, "q#2": 7.0, "q#3": 2.0}
    ev = FakeAgent("Eval", lambda draft: Verdict(score=scores[draft], passed=False, feedback=""))
    wf = BestOfN(gen, ev, n=3, runner=fake_runner, variant_prompt=lambda q, i: f"{q}#{i}")
    result = run(wf.run("q"))
    assert result.meta == {"chosen": 2, "scores": [6.0, 7.0, 2.0]}
    assert result.final_output == "q#2"
    assert len(result.steps) == 6


@pytest.mark.parametrize("kwargs,expected", [({}, 3), ({"max_concurrency": 2}, 2), ({"max_concurrency": 9}, 6)])
def test_best_of_n_bounds_concurrent_calls(kwargs, expected):
    runner = ConcurrentFakeRunner()
    result = run(
        BestOfN(counting_generator(), scripted_evaluator([9]), n=6, runner=runner, **kwargs).run("q")
    )
    assert runner.max_in_flight == expected
    assert runner.in_flight == 0
    assert len(result.steps) == 12


@pytest.mark.parametrize("failure_stage", ["generate", "evaluate", "verdict"])
def test_best_of_n_records_one_failure_and_keeps_successes(failure_stage):
    def generate(text):
        if failure_stage == "generate" and text == "q#2":
            raise RuntimeError("candidate unavailable")
        return text

    def evaluate(draft):
        if draft == "q#2":
            if failure_stage == "verdict":
                return "looks great"
            raise RuntimeError("candidate unavailable")
        return Verdict(score=5 + int(draft[-1]), passed=False, feedback="")

    tracker = RunTracker(base=fake_runner)
    result = run(
        BestOfN(
            FakeAgent("Gen", generate), FakeAgent("Eval", evaluate), runner=tracker,
            variant_prompt=lambda q, i: f"{q}#{i}", max_concurrency=2,
        ).run("q")
    )
    assert result.final_output == "q#3"
    assert result.meta["chosen"] == 3
    assert result.meta["scores"] == [6, None, 8]
    assert result.meta["failures"][0]["candidate"] == 2
    failed_steps = [step for step in result.steps if step.iteration == 2]
    assert failed_steps[-1].name == ("generate" if failure_stage == "generate" else "evaluate")
    assert failed_steps[-1].output == {"error": result.meta["failures"][0]["error"]}
    assert len(result.steps) == len(tracker.calls) == (5 if failure_stage == "generate" else 6)


def test_best_of_n_raises_after_all_candidates_fail():
    def fail(_):
        raise RuntimeError("model down")

    generator = FakeAgent("Gen", fail)
    evaluator = scripted_evaluator([9])
    tracker = RunTracker(base=fake_runner)
    with pytest.raises(RuntimeError, match="All 4 candidates failed") as error:
        run(BestOfN(generator, evaluator, n=4, runner=tracker, max_concurrency=2).run("q"))
    assert len(tracker.calls) == len(generator.calls) == 4
    assert all(call.error == "RuntimeError: model down" for call in tracker.calls)
    assert evaluator.calls == []
    assert isinstance(error.value.__cause__, RuntimeError)


def test_best_of_n_rejects_unstructured_evaluator():
    evaluator = FakeAgent("PlainEvaluator", lambda _: "looks great")
    with pytest.raises(TypeError, match="score/passed/feedback"):
        run(BestOfN(counting_generator(), evaluator, runner=fake_runner).run("q"))
    assert len(evaluator.calls) == 3


@pytest.mark.parametrize("kwargs", [{"n": 0}, {"max_concurrency": 0}, {"max_concurrency": -1}])
def test_best_of_n_validates_limits(kwargs):
    with pytest.raises(ValueError):
        BestOfN(counting_generator(), scripted_evaluator([9]), **kwargs)


def test_best_of_n_preserves_positional_arguments():
    result = run(
        BestOfN(
            FakeAgent("Gen", str.upper), scripted_evaluator([9]), 1, fake_runner,
            lambda q, i: f"{q}#{i}",
        ).run("q")
    )
    assert result.final_output == "Q#1"

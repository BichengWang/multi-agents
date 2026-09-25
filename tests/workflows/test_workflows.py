import asyncio

import pytest

from agent.workflows import BestOfN, RefineLoop, SequentialWorkflow, Step, Verdict

from .fakes import FakeAgent, counting_generator, fake_runner, scripted_evaluator


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

"""Offline tests for the async eval harness."""

import asyncio
import json
import sys

import pytest

from agent.agents_eval.harness import (
    EvalCase,
    KeywordCheck,
    LengthCheck,
    LLMJudgeCheck,
    compare,
    load_cases,
    run_eval,
)
from agent.agents_eval.run import WORKFLOWS, main, story_workflow
from agent.workflows import Verdict
from tests.workflows.fakes import FakeAgent, counting_generator, fake_runner, scripted_evaluator

CASES = [
    EvalCase(id="a", input="story about a map", expected_keywords=["map", "river"]),
    EvalCase(id="b", input="explode"),
]


async def _workflow(query: str) -> str:
    if query == "explode":
        raise RuntimeError("workflow failed")
    return "A map of the river valley."


def test_run_eval_scores_cases_and_isolates_errors():
    checks = [LengthCheck(min_chars=5), KeywordCheck()]
    report = asyncio.run(run_eval("demo", _workflow, CASES, checks))

    by_id = {r.case_id: r for r in report.results}
    assert by_id["a"].passed
    assert by_id["b"].error.startswith("RuntimeError")
    assert not by_id["b"].passed

    summary = report.summary()
    assert summary == {
        "cases": 2,
        "errors": 1,
        "pass_rate": 0.5,
        "mean_scores": {"length": 1.0, "keywords": 1.0},
    }


@pytest.mark.parametrize("concurrency", [0, -1])
def test_run_eval_rejects_nonpositive_concurrency(concurrency):
    async def scenario():
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            await asyncio.wait_for(
                run_eval("demo", _workflow, CASES, [], concurrency=concurrency), timeout=1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [0, -1])
def test_cli_rejects_nonpositive_concurrency_before_loading_dataset(monkeypatch, capsys, concurrency):
    monkeypatch.setattr(sys, "argv", [
        "eval", "--workflow", "story_single", "--dataset", "missing.jsonl",
        "--concurrency", str(concurrency),
    ])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "--concurrency must be >= 1" in capsys.readouterr().err


def test_run_eval_bounds_work_and_preserves_dataset_order():
    async def scenario():
        active = peak = 0
        second_finished = asyncio.Event()

        async def workflow(query):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if query == "0":
                    await second_finished.wait()
                else:
                    await asyncio.sleep(0)
                if query == "1":
                    second_finished.set()
                return query
            finally:
                active -= 1

        cases = [EvalCase(id=str(i), input=str(i)) for i in range(10)]
        report = await run_eval("demo", workflow, cases, [LengthCheck()], concurrency=2)

        assert [result.case_id for result in report.results] == [case.id for case in cases]
        assert [result.output for result in report.results] == [case.input for case in cases]
        assert all(result.passed for result in report.results)
        assert peak == 2 and active == 0

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))


def test_run_eval_case_cancellation_drains_siblings_and_stops_queued_work():
    async def scenario():
        sibling_started = asyncio.Event()
        cleaned = asyncio.Event()
        started = []

        async def workflow(query):
            started.append(query)
            if query == "0":
                await sibling_started.wait()
                raise asyncio.CancelledError()
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        cases = [EvalCase(id=str(i), input=str(i)) for i in range(10)]
        with pytest.raises(asyncio.CancelledError):
            await run_eval("demo", workflow, cases, [], concurrency=2)

        assert cleaned.is_set()
        assert started == ["0", "1"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))


def test_run_eval_isolates_check_errors():
    class BrokenCheck:
        name = "broken"

        async def __call__(self, case, output):
            raise RuntimeError("check failed")

    report = asyncio.run(run_eval("demo", _workflow, CASES[:1], [BrokenCheck(), LengthCheck()]))

    assert not report.results[0].passed
    assert report.results[0].checks[0].score == 0
    assert report.results[0].checks[0].detail == "check error: check failed"
    assert report.results[0].checks[1].passed


def test_keyword_check_partial_match():
    case = EvalCase(id="x", input="", expected_keywords=["map", "dragon"])
    result = asyncio.run(KeywordCheck(min_ratio=0.5)(case, "a MAP"))
    assert result.score == 0.5 and result.passed
    assert "dragon" in result.detail


def test_llm_judge_check_uses_threshold():
    verdict = Verdict(score=6, passed=True, feedback="thin plot")
    judge_agent = FakeAgent("judge", lambda _input: verdict)
    judge = LLMJudgeCheck(judge=judge_agent, threshold=7, runner=fake_runner)
    case = EvalCase(id="x", input="write a story", rubric="must rhyme")

    result = asyncio.run(judge(case, "the output"))

    assert result.score == 0.6 and not result.passed
    assert result.detail == "thin plot"
    assert "must rhyme" in judge_agent.calls[0] and "the output" in judge_agent.calls[0]


def test_story_workflows_return_the_story_not_the_verdict():
    for mode, expected in [("single", "draft-1"), ("refine", "draft-2")]:
        workflow = story_workflow(
            mode,
            runner=fake_runner,
            generator=counting_generator(),
            evaluator=scripted_evaluator([5, 9]),
        )
        assert asyncio.run(workflow("a story")) == expected


def test_save_load_and_compare(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('{"id": "a", "input": "map", "expected_keywords": ["map"]}\n\n')
    cases = load_cases(dataset)
    assert cases == [EvalCase(id="a", input="map", expected_keywords=["map"])]

    async def good(_q):
        return "a map"

    async def bad(_q):
        return "nothing"

    base = asyncio.run(run_eval("base", bad, cases, [KeywordCheck()]))
    cand = asyncio.run(run_eval("cand", good, cases, [KeywordCheck()]))
    saved = json.loads(base.save(tmp_path / "out").read_text())

    assert saved["summary"]["pass_rate"] == 0.0
    assert compare(saved, cand.to_dict()) == {
        "pass_rate_delta": 1.0,
        "mean_score_deltas": {"keywords": 1.0},
    }


def test_bundled_dataset_loads():
    cases = load_cases("agent/agents_eval/datasets/story.jsonl")
    assert len(cases) >= 5
    assert set(WORKFLOWS) == {"story_single", "story_refine", "story_best_of_n"}

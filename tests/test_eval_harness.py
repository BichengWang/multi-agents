"""Offline tests for the async eval harness."""

import asyncio
import json
from types import SimpleNamespace

from agents import Agent

from agent.agents_eval.harness import (
    EvalCase,
    KeywordCheck,
    LengthCheck,
    LLMJudgeCheck,
    compare,
    load_cases,
    run_eval,
)
from agent.agents_eval.run import WORKFLOWS
from agent.core import Evaluation, runner

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


def test_keyword_check_partial_match():
    case = EvalCase(id="x", input="", expected_keywords=["map", "dragon"])
    result = asyncio.run(KeywordCheck(min_ratio=0.5)(case, "a MAP"))
    assert result.score == 0.5 and result.passed
    assert "dragon" in result.detail


def test_llm_judge_check_uses_threshold(monkeypatch):
    verdict = Evaluation(score=6, passed=True, strengths=[], issues=["thin plot"], suggestions="")
    seen = {}

    async def fake_run_agent(agent, input, **_kwargs):
        seen["input"] = input
        return SimpleNamespace(final_output_as=lambda _cls: verdict)

    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    judge = LLMJudgeCheck(judge=Agent(name="judge", instructions="grade"), threshold=7)
    case = EvalCase(id="x", input="write a story", rubric="must rhyme")

    result = asyncio.run(judge(case, "the output"))

    assert result.score == 0.6 and not result.passed
    assert result.detail == "thin plot"
    assert "must rhyme" in seen["input"] and "the output" in seen["input"]


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
    assert set(WORKFLOWS) == {"story_single", "story_refine"}

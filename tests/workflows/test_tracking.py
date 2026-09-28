import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.workflows import RefineLoop, RunTracker, run_record, save_run, to_jsonable

from .fakes import FakeAgent, counting_generator, fake_runner, scripted_evaluator


def test_tracker_records_every_call_through_a_workflow():
    tracker = RunTracker(base=fake_runner)
    loop = RefineLoop(counting_generator(), scripted_evaluator([5, 9]), runner=tracker)

    result = asyncio.run(loop.run("idea"))

    assert [c.agent for c in tracker.calls] == ["draft", "Evaluator", "draft", "Evaluator"]
    assert len(tracker.calls) == len(result.steps)
    assert all(c.duration_s >= 0 and c.error is None and c.usage is None for c in tracker.calls)
    assert tracker.totals()["calls"] == 4
    assert tracker.totals()["total_tokens"] == 0


def test_tracker_records_failed_calls_and_reraises():
    def boom(_input):
        raise RuntimeError("model down")

    tracker = RunTracker(base=fake_runner)
    with pytest.raises(RuntimeError):
        asyncio.run(tracker(FakeAgent("flaky", boom), "hi"))

    assert tracker.calls[0].error == "RuntimeError: model down"
    assert tracker.totals()["errors"] == 1


def test_tracker_sdk_mode_captures_token_usage(monkeypatch):
    agents = pytest.importorskip("agents")
    usage = agents.Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)

    async def fake_run(agent, input_text, **_kwargs):
        return SimpleNamespace(final_output=f"echo {input_text}", context_wrapper=SimpleNamespace(usage=usage))

    monkeypatch.setattr(agents.Runner, "run", fake_run)
    tracker = RunTracker()

    output = asyncio.run(tracker(agents.Agent(name="Writer", instructions="x"), "hi"))

    assert output == "echo hi"
    assert tracker.calls[0].usage == {"requests": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert tracker.totals()["total_tokens"] == 15


def test_save_run_writes_a_json_record(tmp_path):
    tracker = RunTracker(base=fake_runner)
    result = asyncio.run(RefineLoop(counting_generator(), scripted_evaluator([9]), runner=tracker).run("idea"))

    record = run_record(result, workflow="story/refine", query="idea", started_at="t0", duration_s=1.23456, tracker=tracker)
    path = save_run(record, tmp_path / "runs")

    assert path.name.endswith("_story-refine.json")
    saved = json.loads(path.read_text())
    assert saved["final_output"] == "draft-1"
    assert saved["verdict"] == {"score": 9.0, "passed": True, "feedback": "feedback for 9"}
    assert [s["name"] for s in saved["steps"]] == ["generate", "evaluate"]
    assert saved["steps"][1]["output"]["score"] == 9.0
    assert saved["totals"]["calls"] == 2
    assert saved["duration_s"] == 1.235


def test_to_jsonable_falls_back_to_str_for_unknown_objects():
    class Opaque:
        def __str__(self):
            return "opaque"

    assert to_jsonable({"a": (1, Opaque()), 2: None}) == {"a": [1, "opaque"], "2": None}

import asyncio
import json
from contextlib import nullcontext

import pytest

pytest.importorskip("agents")

from agent.triage.classifier_agent import classifier_agent  # noqa: E402
from agent.triage.manager import TriageManager, build_routes  # noqa: E402
from agent.workflows import CheckpointDecision, RouteDecision, Verdict  # noqa: E402


def test_classifier_has_structured_output():
    assert classifier_agent.output_type is RouteDecision


def test_routes_cover_every_specialist():
    assert [r.name for r in build_routes()] == ["story", "store", "financial"]


def test_triage_runs_store_pipeline_with_injected_runner(capsys):
    calls = []

    async def runner(agent, input_text):
        calls.append(agent.name)
        if agent is classifier_agent:
            return RouteDecision(route="store", confidence=0.8, reason="business idea")
        return f"{agent.name} output"

    result = asyncio.run(TriageManager(runner=runner).run("a campus coffee shop"))

    assert calls[0] == "TriageClassifierAgent"
    assert calls[1:] == [
        "StoreGeneratorAgent", "StoreExplainerAgent", "StoreEvaluatorAgent", "StoreManagerAgent",
    ]
    assert result.meta["route"] == "store"
    assert [s.name for s in result.steps] == ["classify", "generate", "explain", "evaluate", "plan"]
    assert "Routed to 'store'" in capsys.readouterr().out


def test_triage_forwards_checkpoint_and_preserves_story_metadata():
    calls = []

    async def runner(agent, input_text):
        calls.append(agent.name)
        if agent.name == "StoryEvaluatorAgent":
            return Verdict(score=5, passed=False, feedback="revise the ending")
        return "a moon heist"

    async def checkpoint(state):
        assert state.query == "idea" and state.draft == "a moon heist"
        return CheckpointDecision(continue_refining=False)

    result = asyncio.run(
        TriageManager(runner=runner, story_checkpoint=checkpoint).run("idea", route="story")
    )
    assert len(calls) == 2
    assert result.final_output == "a moon heist"
    assert result.meta["route_meta"]["checkpoint_stopped"] is True
    assert [step.name for step in result.steps] == ["generate", "evaluate", "checkpoint"]


def test_shared_cli_saves_human_stop_in_run_artifact(monkeypatch, tmp_path):
    import agent.main as entrypoint
    from agent.workflows import RunTracker

    calls = []

    async def runner(agent, input_text):
        calls.append(agent.name)
        if agent.name == "StoryEvaluatorAgent":
            return Verdict(score=5, passed=False, feedback="revise the ending")
        return "a moon heist"

    monkeypatch.setattr(entrypoint, "RunTracker", lambda: RunTracker(base=runner))
    monkeypatch.setattr("agents.trace", lambda _: nullcontext())
    monkeypatch.setattr("builtins.input", lambda _: "/stop")
    monkeypatch.setattr(
        "sys.argv",
        ["agent", "idea", "--route", "story", "--story-checkpoint", "--runs-dir", str(tmp_path)],
    )
    asyncio.run(entrypoint.main())

    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["final_output"] == "a moon heist"
    assert record["meta"]["route_meta"]["checkpoint_stopped"] is True
    assert record["steps"][-1]["output"] == {"continue_refining": False, "feedback": ""}
    assert record["totals"]["calls"] == len(calls) == 2

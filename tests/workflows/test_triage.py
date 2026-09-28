import asyncio

import pytest

pytest.importorskip("agents")

from agent.triage.classifier_agent import classifier_agent  # noqa: E402
from agent.triage.manager import TriageManager, build_routes  # noqa: E402
from agent.workflows import RouteDecision  # noqa: E402


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

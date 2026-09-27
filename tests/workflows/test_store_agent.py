import asyncio

import pytest

pytest.importorskip("agents")

from agent.store_agent.manager import StoreAgentManager  # noqa: E402


def test_store_pipeline_chains_four_stages_in_order():
    calls = []

    async def runner(agent, input_text):
        calls.append((agent.name, input_text))
        return f"{agent.name} output"

    result = asyncio.run(StoreAgentManager(runner=runner).run("campus coffee shop"))

    assert [name for name, _ in calls] == [
        "StoreGeneratorAgent",
        "StoreExplainerAgent",
        "StoreEvaluatorAgent",
        "StoreManagerAgent",
    ]
    # Each stage receives the previous stage's output.
    assert calls[0][1] == "campus coffee shop"
    assert [inp for _, inp in calls[1:]] == [f"{name} output" for name, _ in calls[:-1]]
    assert [s.name for s in result.steps] == ["generate", "explain", "evaluate", "plan"]
    assert result.final_output == "StoreManagerAgent output"

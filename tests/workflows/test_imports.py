import importlib

import pytest

pytest.importorskip("agents")


@pytest.mark.parametrize(
    "module",
    [
        "agent.workflows",
        "agent.story_agent_simple.manager",
        "agent.story_agent.manager",
        "agent.financial_research_agent.manager",
    ],
)
def test_agent_packages_import(module):
    importlib.import_module(module)

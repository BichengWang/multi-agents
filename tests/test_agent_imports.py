"""Offline smoke tests: every agent module imports and its agents are wired correctly.

No model calls are made; OPENAI_API_KEY only needs to be set to a placeholder.
"""

import importlib

import pytest
from agents import Agent, Handoff

AGENT_MODULES = [
    "agent.financial_research_agent.main",
    "agent.store_agent.main",
    "agent.store_agent.my_agents.coordinator_agent",
    "agent.story_agent_simple.main",
    "agent.fix_agents.financial.financial_adviser",
]


@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_module_imports(module_name):
    importlib.import_module(module_name)


def _assert_valid_handoffs(agent: Agent):
    for target in agent.handoffs:
        assert isinstance(target, (Agent, Handoff)), f"{agent.name} has invalid handoff {target!r}"


def test_coordinator_handoffs_are_agents():
    from agent.store_agent.my_agents.coordinator_agent import coordinator_agent

    assert len(coordinator_agent.handoffs) == 4
    _assert_valid_handoffs(coordinator_agent)


def test_financial_adviser_handoffs_are_agents():
    from agent.fix_agents.financial.financial_adviser import create_agents

    for agent in create_agents().values():
        _assert_valid_handoffs(agent)

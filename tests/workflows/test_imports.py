import importlib

import pytest

pytest.importorskip("agents")


@pytest.mark.parametrize("module", ["agent.workflows", "agent.teams"])
def test_agent_packages_import(module):
    importlib.import_module(module)

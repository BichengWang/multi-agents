import os

# Central model registry. Override any role with an env var, e.g.
# `AGENT_MODEL_WRITER=gpt-5 python -m agent.financial_research_agent.main`.
_DEFAULTS = {
    "default": "gpt-5-mini",
    "planner": "gpt-5-mini",
    "writer": "gpt-5",
    "verifier": "gpt-5-mini",
    "judge": "gpt-5",
    "router": "gpt-5-mini",
}


def model_for(role: str = "default") -> str:
    """Return the model name for an agent role, honoring AGENT_MODEL_<ROLE> overrides."""
    env_value = os.getenv(f"AGENT_MODEL_{role.upper()}")
    if env_value:
        return env_value
    if role in _DEFAULTS:
        return _DEFAULTS[role]
    return model_for("default")

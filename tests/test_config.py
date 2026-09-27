from agent.config import model_for


def test_known_role_default():
    assert model_for("writer")


def test_role_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_WRITER", "custom-writer")
    assert model_for("writer") == "custom-writer"


def test_unknown_role_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_DEFAULT", "custom-default")
    assert model_for("brand-new-role") == "custom-default"

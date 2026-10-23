import pytest

from agent.bench.docker_backend import DockerBackend
from agent.bench.sandbox import SandboxConfig, SandboxError


def test_missing_api_keys_fail_before_any_container_starts_and_are_named():
    config = SandboxConfig(secret_keys=("ANTHROPIC_API_KEY", "CODEX_API_KEY"))
    with pytest.raises(SandboxError, match="CODEX_API_KEY") as info:
        DockerBackend(config, environ={"ANTHROPIC_API_KEY": "sk-ant-x", "CODEX_API_KEY": ""})
    assert "ANTHROPIC_API_KEY" not in str(info.value)

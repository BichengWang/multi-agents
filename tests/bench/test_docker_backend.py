import asyncio

import pytest

from agent.bench import docker_backend
from agent.bench.docker_backend import DockerBackend
from agent.bench.sandbox import SandboxConfig, SandboxError


def test_missing_api_keys_fail_before_any_container_starts_and_are_named():
    config = SandboxConfig(secret_keys=("ANTHROPIC_API_KEY", "CODEX_API_KEY"))
    with pytest.raises(SandboxError, match="CODEX_API_KEY") as info:
        DockerBackend(config, environ={"ANTHROPIC_API_KEY": "sk-ant-x", "CODEX_API_KEY": ""})
    assert "ANTHROPIC_API_KEY" not in str(info.value)


@pytest.mark.parametrize(
    ("environ", "passed"),
    [
        ({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x"}, "CLAUDE_CODE_OAUTH_TOKEN"),
        ({"ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x"}, "ANTHROPIC_API_KEY"),
    ],
)
def test_subscription_token_reaches_agent_containers_only_without_an_api_key(monkeypatch, environ, passed):
    calls = []

    async def fake_docker_ok(*args, env=None):
        calls.append((args, env))
        return b"container-id\n"

    async def fake_image(tag, *, agent_tools):
        return tag

    monkeypatch.setattr(docker_backend.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_backend, "_docker_ok", fake_docker_ok)
    backend = DockerBackend(SandboxConfig(secret_keys=("ANTHROPIC_API_KEY",)), environ=environ)
    monkeypatch.setattr(backend, "_ensure_image", fake_image)

    asyncio.run(backend.create_sandbox("img", agent=True))
    asyncio.run(backend.create_sandbox("img", agent=False))

    (agent_args, agent_env), (grade_args, grade_env) = calls
    other = ({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} - {passed}).pop()
    assert agent_env[passed] == environ[passed] and passed in agent_args
    assert other not in agent_env and other not in agent_args
    assert passed not in grade_env and passed not in grade_args


def test_missing_claude_key_names_the_subscription_alternative():
    with pytest.raises(SandboxError, match=r"ANTHROPIC_API_KEY \(or CLAUDE_CODE_OAUTH_TOKEN\)"):
        DockerBackend(SandboxConfig(secret_keys=("ANTHROPIC_API_KEY",)), environ={})


def test_a_command_past_its_deadline_is_killed_in_its_container_and_becomes_a_timeout(monkeypatch):
    from agent.bench.sandbox import SandboxExecTimeout, SandboxWorkspace

    docker_calls, procs = [], []

    async def fake_docker(*args, env=None):
        docker_calls.append(args)  # never run: `kill -KILL -1` here would hit the host
        return 0, b"", b""

    class LocalContainer:
        async def exec(self, *argv, workdir, env=None, timeout, text=False):
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            procs.append(proc)
            return docker_backend._DockerProcess(proc, "c1", timeout)

    monkeypatch.setattr(docker_backend, "_docker", fake_docker)

    async def go():
        with pytest.raises(SandboxExecTimeout):
            await SandboxWorkspace._raw_exec(
                LocalContainer(), SandboxConfig(), ["sleep", "30"], workdir="/", timeout=0.2
            )
        # A command that finishes in time keeps its own exit code.
        code, out, _ = await SandboxWorkspace._raw_exec(
            LocalContainer(), SandboxConfig(), ["sh", "-c", "echo hi; exit 3"], workdir="/", timeout=30
        )
        assert (code, out) == (3, b"hi\n")

    asyncio.run(go())
    assert docker_calls == [("exec", "c1", "sh", "-c", "kill -KILL -1")]
    assert procs[0].returncode is not None  # the local `docker exec` client is gone too

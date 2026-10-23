"""Local Docker implementation of the sandbox backend used by ``SandboxWorkspace``.

Runs the same instance images, setup, diff and grading scripts as the Modal backend, on any Docker
daemon (Docker Desktop, Colima, a Linux host). Images are ``linux/amd64``; on Apple silicon run
the daemon with Rosetta (``colima start --vz-rosetta``) or builds and tests are slow.

* Agent containers extend the instance image with the pinned Node.js and CLIs once per image
  (``docker run`` + ``docker commit``, no builder plugin needed), cached as
  ``multi-agents-bench-agent:<hash>``. API keys are the host's environment variables named in
  ``SandboxConfig.secret_keys``; grading containers get none.
* Every container runs ``sleep <lifetime>`` as PID 1 with ``--rm``, so it disappears when its
  lifetime ends even if the run is killed.
* ``exec`` timeouts are enforced by the client: at the deadline every process in the container
  except PID 1 is killed (``kill -KILL -1``) and ``wait()`` reports ``-1``, the convention
  ``SandboxWorkspace`` already maps to ``SandboxExecTimeout``. A workspace runs one command at a
  time, so nothing else is caught by that kill. PID 1 is ``sleep``, as on Modal, so the killed
  processes stay as zombies until the container is removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
from typing import Optional

from .redact import Redactor
from .sandbox import _SANDBOX_ENV, SandboxConfig, SandboxError, install_script


PLATFORM = "linux/amd64"
AGENT_IMAGE_REPO = "multi-agents-bench-agent"
LABEL = "multi-agents-bench=1"


async def _docker(
    *args: str, env: Optional[dict[str, str]] = None
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env} if env else None,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def _docker_ok(*args: str, env: Optional[dict[str, str]] = None) -> bytes:
    code, out, err = await _docker(*args, env=env)
    if code != 0:
        raise SandboxError(
            f"docker {' '.join(args[:2])} failed ({code}): {err.decode('utf-8', 'replace').strip()[-2000:]}"
        )
    return out


class _DockerProcess:
    """One ``docker exec``: the stream interface ``SandboxWorkspace._raw_exec`` drives."""

    def __init__(self, proc: asyncio.subprocess.Process, container: str, timeout: float) -> None:
        self._proc = proc
        self._container = container
        self._timeout = timeout
        self._code: Optional[int] = None
        self._waiter: Optional[asyncio.Future[int]] = None
        self.stdin = proc.stdin
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    async def _wait(self) -> int:
        try:
            return await asyncio.wait_for(asyncio.shield(self._proc.wait()), self._timeout)
        except asyncio.TimeoutError:
            # Killing the local client does not stop the process inside the container.
            await _docker("exec", self._container, "sh", "-c", "kill -KILL -1")
            if self._proc.returncode is None:
                self._proc.kill()
            await self._proc.wait()
            return -1

    async def wait(self) -> int:
        if self._waiter is None:
            self._waiter = asyncio.ensure_future(self._wait())
        return await asyncio.shield(self._waiter)


class DockerSandbox:
    def __init__(self, container: str) -> None:
        self.container = container

    async def exec(
        self,
        *argv: str,
        workdir: str,
        env: Optional[dict[str, str]] = None,
        timeout: float,
        text: bool = False,
    ) -> _DockerProcess:
        assert text is False
        flags: list[str] = ["-i", "-w", workdir]
        for key, value in (env or {}).items():
            flags += ["-e", f"{key}={value}"]
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            *flags,
            self.container,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return _DockerProcess(proc, self.container, timeout)

    async def terminate(self) -> None:
        code, _, err = await _docker("rm", "-f", self.container)
        if code != 0 and b"No such container" not in err:
            raise SandboxError(f"docker rm failed ({code}): {err.decode('utf-8', 'replace').strip()}")


class DockerBackend:
    """Starts instance containers on the local Docker daemon."""

    def __init__(
        self, config: Optional[SandboxConfig] = None, *, environ: Optional[dict[str, str]] = None
    ) -> None:
        self.config = config or SandboxConfig()
        self.redactor = Redactor()
        environ = dict(os.environ if environ is None else environ)
        missing = [k for k in self.config.secret_keys if not environ.get(k)]
        if missing:
            raise SandboxError(
                "the docker backend passes API keys from your environment; set "
                + ", ".join(missing)
            )
        if shutil.which("docker") is None:
            raise SandboxError("docker is not on PATH")
        self._secrets = {k: environ[k] for k in self.config.secret_keys}
        self._image_locks: dict[str, asyncio.Lock] = {}

    def agent_image_tag(self, tag: str) -> str:
        digest = hashlib.sha256(f"{tag}\n{install_script(self.config)}".encode()).hexdigest()
        return f"{AGENT_IMAGE_REPO}:{digest[:20]}"

    async def _exists(self, tag: str) -> bool:
        code, _, _ = await _docker("image", "inspect", "--format", "{{.Id}}", tag)
        return code == 0

    async def _ensure_image(self, tag: str, *, agent_tools: bool) -> str:
        lock = self._image_locks.setdefault(tag, asyncio.Lock())
        async with lock:
            if not await self._exists(tag):
                await _docker_ok("pull", "--quiet", "--platform", PLATFORM, tag)
        if not agent_tools:
            return tag
        derived = self.agent_image_tag(tag)
        lock = self._image_locks.setdefault(derived, asyncio.Lock())
        async with lock:
            if not await self._exists(derived):
                await self._build_agent_image(tag, derived)
        return derived

    async def _build_agent_image(self, tag: str, derived: str) -> None:
        name = f"mab-build-{derived.rpartition(':')[2]}"
        await _docker("rm", "-f", name)
        try:
            await _docker_ok(
                "run", "--name", name, "--platform", PLATFORM, "--label", LABEL,
                "--entrypoint", "sh", tag, "-c", install_script(self.config),
            )
            await _docker_ok("commit", name, derived)
        finally:
            await _docker("rm", "-f", name)

    async def create_sandbox(self, tag: str, *, agent: bool) -> DockerSandbox:
        cfg = self.config
        image = await self._ensure_image(tag, agent_tools=agent)
        if agent:
            env = {**_SANDBOX_ENV, **cfg.env, **self._secrets}
        else:
            env = {"TERM": "xterm-mono", "DEBIAN_FRONTEND": "noninteractive"}
        # Names only: docker reads the values from its own environment, so keys stay out of `ps`.
        names = [flag for key in env for flag in ("-e", key)]
        out = await _docker_ok(
            "run", "-d", "--rm", "--platform", PLATFORM, "--label", LABEL,
            # Modal counts physical cores (2 vCPU each); docker counts CPUs.
            "--cpus", f"{cfg.cpu * 2:g}", "--memory", f"{cfg.memory_mb}m",
            *names, "--entrypoint", "sleep", image, str(max(1, math.ceil(cfg.lifetime_s))),
            env=env,
        )
        return DockerSandbox(out.decode().strip())

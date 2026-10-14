"""Offline fakes: a Modal client that runs sandbox commands in local temporary git repositories,
and in-memory ``Team`` / ``Workspace`` implementations of the frozen contract."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from agent.teams.types import ExecResult, HarnessRun, TeamResult, Usage

# --------------------------------------------------------------------------------------------
# Fake Modal
# --------------------------------------------------------------------------------------------


def git(cwd: str | Path, *args: str, env: Optional[dict[str, str]] = None) -> str:
    full_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
                "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false",
                **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=full_env, check=True, capture_output=True, text=True
    ).stdout


def make_repo(path: Path, files: dict[str, str], *, nested: Optional[str] = None) -> Path:
    """Create a git repo with one commit; ``nested`` puts it in a subdirectory of ``path``."""
    root = path / nested if nested else path
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


class _Stream:
    def __init__(self, proc: "FakeProcess", which: str) -> None:
        self._proc, self._which = proc, which

    async def read(self) -> bytes:
        await self._proc._finish()
        return self._proc._out if self._which == "out" else self._proc._err


class _StdinWriter:
    def __init__(self, proc: "FakeProcess") -> None:
        self._proc = proc

    def write(self, data: bytes) -> None:
        self._proc._stdin_data += data

    def write_eof(self) -> None:
        self._proc._stdin_closed = True

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], timeout: Optional[float]):
        self._argv, self._cwd, self._env, self._timeout = argv, cwd, env, timeout
        self._stdin_data = b""
        self._stdin_closed = False
        self._out = self._err = b""
        self._code: Optional[int] = None
        self._done: Optional[asyncio.Future] = None
        self.stdin = _StdinWriter(self)
        self.stdout = _Stream(self, "out")
        self.stderr = _Stream(self, "err")

    async def _finish(self) -> None:
        # stdout/stderr/wait are awaited concurrently; run the command exactly once.
        if self._done is None:
            self._done = asyncio.ensure_future(self._run())
        await asyncio.shield(self._done)

    async def _run(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        comm = asyncio.ensure_future(proc.communicate(self._stdin_data))
        done, _ = await asyncio.wait({comm}, timeout=self._timeout)
        if not done:
            # Like modal-client (py/v1.6.0): the deadline raises nothing; the process is killed,
            # the streams end and wait() reports -1.
            proc.kill()
            await proc.wait()
            self._out, self._err = await comm
            self._code = -1
            return
        out, err = comm.result()
        self._out, self._err, self._code = out, err, proc.returncode or 0

    async def wait(self) -> int:
        await self._finish()
        assert self._code is not None
        return self._code


class FakeSandbox:
    """Runs exec'd commands locally. ``/testbed`` and ``/tmp/bench-`` are redirected into a
    private directory and HOME is private, so nothing touches the real machine state."""

    def __init__(self, template: Path, workroot: Path, image_tag: str, env: dict[str, str]):
        self.image_tag = image_tag
        self.workroot = workroot
        self.testbed = workroot / "repo"  # must not contain the substring "/testbed"
        shutil.copytree(template, self.testbed, symlinks=True)
        (workroot / "home").mkdir()
        self.env = env
        self.terminated = False
        self.terminate_failures = 0
        self.exec_log: list[list[str]] = []

    def _map(self, text: str) -> str:
        return text.replace("/testbed", str(self.testbed)).replace(
            "/tmp/bench-", str(self.workroot) + "/bench-"
        )

    async def exec(self, *argv: str, workdir=None, env=None, timeout=None, text=True, **_: Any):
        assert not self.terminated, "exec on a terminated sandbox"
        assert text is False
        self.exec_log.append(list(argv))
        mapped = [self._map(a) for a in argv]
        cwd = self._map(workdir) if workdir and workdir != "/" else str(self.testbed)
        full_env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.workroot / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            **self.env,
            **(env or {}),
        }
        return FakeProcess(mapped, cwd, full_env, timeout)

    async def terminate(self, **_: Any) -> None:
        if self.terminate_failures > 0:
            self.terminate_failures -= 1
            raise RuntimeError("modal unavailable")
        self.terminated = True


@dataclass
class FakeImage:
    tag: str
    commands: list[str] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)

    def run_commands(self, *commands: str) -> "FakeImage":
        return FakeImage(self.tag, [*self.commands, *commands], self.kwargs)


class FakeModal:
    """Just enough of the ``modal`` module for ``ModalBackend``."""

    def __init__(
        self,
        template: Path,
        workroot: Path,
        *,
        secret_env: Optional[dict[str, str]] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> None:
        self.template, self.workroot = template, workroot
        self.secret_env = secret_env or {}
        self.extra_env = extra_env or {}
        self.sandboxes: list[FakeSandbox] = []
        self.created: list[dict[str, Any]] = []
        self.secrets: list[tuple[str, list[str]]] = []
        outer = self

        async def lookup(name: str, create_if_missing: bool = False):
            return SimpleNamespace(name=name)

        async def create(*args: str, app=None, image=None, env=None, secrets=None, **kwargs):
            outer.created.append(
                {"args": args, "image": image, "env": env or {}, "secrets": secrets, **kwargs}
            )
            sandbox_env = {**(env or {}), **outer.extra_env}
            if secrets:
                sandbox_env.update(outer.secret_env)
            sb = FakeSandbox(
                outer.template, Path(tempfile.mkdtemp(dir=outer.workroot)), image.tag, sandbox_env
            )
            outer.sandboxes.append(sb)
            return sb

        def from_registry(tag: str, **kwargs: Any) -> FakeImage:
            return FakeImage(tag, kwargs=kwargs)

        def secret_from_name(name: str, required_keys=None) -> Any:
            outer.secrets.append((name, list(required_keys or [])))
            return SimpleNamespace(secret=name)

        self.App = SimpleNamespace(lookup=lookup)
        self.Sandbox = SimpleNamespace(create=create)
        self.Image = SimpleNamespace(from_registry=from_registry)
        self.Secret = SimpleNamespace(from_name=secret_from_name)


# --------------------------------------------------------------------------------------------
# Fake Team / Workspace (contract-level)
# --------------------------------------------------------------------------------------------


class MemoryWorkspace:
    """In-memory ``Workspace``: ``diff`` returns whatever the fake team 'edited'."""

    created: list["MemoryWorkspace"] = []
    isolate_history = True
    head = "a" * 40

    def __init__(self, *, diff_fails: bool = False) -> None:
        self.root = "/memory"
        self.edits: list[str] = []
        self.closed = False
        self.diff_fails = diff_fails
        MemoryWorkspace.created.append(self)

    async def exec(self, argv, *, env=None, timeout=None, stdin=None) -> ExecResult:
        return ExecResult(0, "", "")

    async def diff(self) -> str:
        if self.diff_fails:
            raise RuntimeError("sandbox is gone")
        return "".join(self.edits)

    async def apply(self, patch: str) -> None:
        self.edits.append(patch)

    async def fork(self) -> "MemoryWorkspace":
        return MemoryWorkspace()

    async def close(self) -> None:
        self.closed = True


def make_run(cost: Optional[float], role: str = "worker", seconds: float = 1.5) -> HarnessRun:
    return HarnessRun(
        harness="claude", model="m", role=role, final_message="done", exit_code=0,
        usage=Usage(10, 2, 5, cost), duration_s=seconds, session_id="s", log="LOG",
    )


class FakeTeam:
    def __init__(
        self,
        name: str,
        *,
        cost: Optional[float] = 0.5,
        patch: Callable[[str], str] = lambda task_id: f"diff --git a/{task_id} b/{task_id}\n",
        delay: float = 0.0,
        fail_for: Optional[set[str]] = None,
        partial: str = "",
        log: str = "LOG",
    ) -> None:
        self.name = name
        self.cost = cost
        self.patch = patch
        self.delay = delay
        self.fail_for = fail_for or set()
        self.partial = partial  # edits made before the delay / crash
        self.log = log
        self.solved: list[str] = []
        self.tasks_seen: list[Any] = []
        self.active = 0
        self.peak = 0

    async def solve(self, task, workspace) -> TeamResult:
        self.tasks_seen.append(task)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.partial:
                workspace.edits.append(self.partial)
            if self.delay:
                await asyncio.sleep(self.delay)
            if task.task_id in self.fail_for:
                raise RuntimeError("agent crashed")
            self.solved.append(task.task_id)
            patch = self.patch(task.task_id)
            workspace.edits.append(patch)
            run = make_run(self.cost)
            run.log = self.log
            return TeamResult(patch=await workspace.diff(), runs=[run], duration_s=2.0)
        finally:
            self.active -= 1

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from agent.teams import ExecResult, HarnessRun, Usage, Workspace


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return out.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("base\n")
    (path / ".gitignore").write_text("*.log\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def run(coro):
    return asyncio.run(coro)


Script = Callable[["Call"], Awaitable[str]]


@dataclass
class Call:
    workspace: Workspace
    prompt: str
    role: str
    session_id: Optional[str]
    number: int


@dataclass
class FakeHarness:
    """Harness whose behaviour is a coroutine over the real workspace it is given."""

    script: Script
    name: str = "fake"
    model: Optional[str] = "m"
    calls: list[Call] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    async def run(self, workspace, prompt, *, role="worker", session_id=None, timeout=None) -> HarnessRun:
        call = Call(workspace, prompt, role, session_id, len(self.calls) + 1)
        self.calls.append(call)
        self.roots.append(workspace.root)
        message = await self.script(call)
        return HarnessRun(
            self.name, self.model, role, message, 0, Usage(10, 0, 5, 0.01), 0.0, f"session-{self.name}"
        )


async def write(workspace: Workspace, path: str, content: str) -> None:
    res = await workspace.exec(["sh", "-c", 'printf %s "$1" > "$2"', "sh", content, path])
    assert res.exit_code == 0, res.stderr


async def read(workspace: Workspace, path: str) -> str:
    return (await workspace.exec(["cat", path])).stdout


def verdict_json(score: float, passed: bool = False, feedback: str = "fb") -> str:
    return f'Here is my verdict:\n{{"score": {score}, "passed": {str(passed).lower()}, "feedback": "{feedback}"}}'


@dataclass
class ScriptedWorkspace:
    """Workspace that answers exec calls from a function; nothing touches the disk."""

    handler: Callable[[list[str], Optional[str]], ExecResult]
    root: str = "/work/repo"
    calls: list[list[str]] = field(default_factory=list)

    async def exec(self, argv, *, env=None, timeout=None, stdin=None) -> ExecResult:
        self.calls.append(argv)
        return self.handler(argv, stdin)

    async def diff(self) -> str:
        return ""

    async def apply(self, patch: str) -> None:
        pass

    async def fork(self):
        return self

    async def close(self) -> None:
        pass

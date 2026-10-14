"""Contracts shared by the team runner and the benchmark.

A ``Team`` solves a ``CodingTask`` inside a ``Workspace`` by driving one or more ``Harness``es:
external coding-agent CLIs or the built-in SDK agent. The same team code runs against local git
worktrees and against remote benchmark sandboxes, so harnesses and teams only touch the task
repository through ``Workspace.exec``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class Workspace(Protocol):
    """An isolated checkout of the task repository, created at the task's base commit.

    ``root`` is the repository root as seen by processes started with ``exec``. ``exec`` merges
    ``env`` over the workspace's own environment, runs with ``root`` as the working directory,
    and kills the process on timeout or cancellation.

    ``root`` is a git checkout at the base commit with the base objects present: teams run ``git``
    through ``exec`` (the ``plan`` team merges patches with ``git apply --3way``, which needs the
    base blobs), so an implementation must provide that, not just a directory of files.
    """

    root: str

    async def exec(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        stdin: Optional[str] = None,
    ) -> ExecResult: ...

    async def diff(self) -> str:
        """Binary-safe unified diff of every change since the base commit, new files included."""
        ...

    async def apply(self, patch: str) -> None:
        """Apply a patch produced by ``diff`` to this workspace."""
        ...

    async def fork(self) -> Workspace:
        """Create an independent workspace at the base commit (not at the current state)."""
        ...

    async def close(self) -> None: ...


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    # None when the harness neither reports nor can price the call; any unpriced part makes a
    # sum unpriced, so totals never silently under-report cost.
    cost_usd: Optional[float] = None

    def __add__(self, other: Usage) -> Usage:
        if self.cost_usd is None or other.cost_usd is None:
            cost = None
        else:
            cost = self.cost_usd + other.cost_usd
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=cost,
        )


@dataclass
class HarnessRun:
    """One non-interactive harness invocation and what it consumed."""

    harness: str
    model: Optional[str]
    role: str
    final_message: str
    exit_code: int
    usage: Usage
    duration_s: float
    session_id: Optional[str] = None
    log: str = ""


@runtime_checkable
class Harness(Protocol):
    """Runs a coding agent to completion in a workspace.

    ``session_id`` resumes a previous session of the same harness in the same workspace when the
    harness supports it. ``role`` labels the run in results (``worker``, ``coder``, ``reviewer``,
    ``judge``, ``planner``, ``integrator``).
    """

    name: str
    model: Optional[str]

    async def run(
        self,
        workspace: Workspace,
        prompt: str,
        *,
        role: str = "worker",
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HarnessRun: ...


@dataclass(frozen=True)
class CodingTask:
    task_id: str
    instruction: str


@dataclass
class TeamResult:
    """The patch a team produced, with every harness run that went into it."""

    patch: str
    runs: list[HarnessRun] = field(default_factory=list)
    duration_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def usage(self) -> Usage:
        total = Usage(cost_usd=0.0)
        for run in self.runs:
            total = total + run.usage
        return total


@runtime_checkable
class Team(Protocol):
    """A team topology. ``name`` is the canonical team spec, used to label results."""

    name: str

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult: ...

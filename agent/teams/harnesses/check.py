"""Objective checks: a shell command (tests, build, lint) that judges a patch by its exit code.

``Check`` is the command plus its timeout; ``Check.bind(workspace)`` gives a ``CheckRunner`` that
runs it in a throwaway fork of ``workspace`` with a patch applied (results are memoized per patch).
``CheckHarness`` is the same command as a pseudo-harness, usable as ``reviewer=check`` or
``judge=check``: it runs in the fork the reviewer/judge machinery already provides and answers with
a verdict (10 if the command exits 0, else 0, feedback = the tail of its output). No LLM is called,
so a run costs exactly $0.00.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from agent.workflows import Verdict

from ..types import HarnessRun, Usage, Workspace
from ..worktree import close_to_completion
from .base import run_cli

TAIL_CHARS = 4000


@dataclass
class CheckResult:
    command: str
    passed: bool
    exit_code: Optional[int]  # None when the command did not finish (timeout, no `sh`, no patch)
    output_tail: str
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        """One-paragraph account for prompts: outcome, then the tail of the output."""
        status = "PASSED" if self.passed else "FAILED"
        code = f"exit code {self.exit_code}" if self.exit_code is not None else "did not finish"
        output = self.output_tail.strip() or "(no output)"
        # A fence longer than any backtick run in the output cannot be closed by the output itself.
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", output)), default=0) + 1)
        return f"`{self.command}` {status} ({code}). Output (tail):\n{fence}\n{output}\n{fence}"


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text if len(text) <= limit else "... " + text[-limit:]


def _join_streams(stdout: str, stderr: str) -> str:
    if stdout and stderr and not stdout.endswith("\n"):
        return f"{stdout}\n{stderr}"
    return f"{stdout}{stderr}"


async def execute_check(workspace: Workspace, command: str, timeout: Optional[float]) -> CheckResult:
    """Run ``command`` through ``sh -c`` in ``workspace``; a timeout is a failed check."""
    outcome = await run_cli(workspace, ["sh", "-c", command], timeout=timeout)
    if outcome.result is None:
        output = _join_streams(outcome.partial_stdout, outcome.partial_stderr)
        text = f"[{outcome.error}]\n{output}" if output else f"[{outcome.error}]"
        return CheckResult(command, False, None, tail(text), outcome.duration_s)
    out = outcome.result
    return CheckResult(
        command,
        out.exit_code == 0,
        out.exit_code,
        tail(_join_streams(out.stdout, out.stderr)),
        outcome.duration_s,
    )


@dataclass(frozen=True)
class Check:
    command: str
    timeout: Optional[float] = None

    def bind(self, workspace: Workspace) -> CheckRunner:
        return CheckRunner(self, workspace)


class CheckRunner:
    """Runs a check against patches of one workspace, each in its own short-lived fork."""

    def __init__(self, check: Check, workspace: Workspace) -> None:
        self.check = check
        self.workspace = workspace
        self._results: dict[str, CheckResult] = {}

    async def __call__(self, patch: str) -> CheckResult:
        if patch in self._results:
            return self._results[patch]
        if not patch.strip():
            result = CheckResult(self.check.command, False, None, "No changes to check.", 0.0)
        else:
            result = await self._run(patch)
        self._results[patch] = result
        return result

    async def _run(self, patch: str) -> CheckResult:
        fork: Optional[Workspace] = None
        try:
            fork = await self.workspace.fork()
            await fork.apply(patch)
            return await execute_check(fork, self.check.command, self.check.timeout)
        except Exception as exc:
            return CheckResult(
                self.check.command, False, None, f"The check could not run ({type(exc).__name__}: {exc})", 0.0
            )
        finally:
            if fork is not None:
                await close_to_completion(fork)


@dataclass
class CheckHarness:
    """The check as a reviewer/judge.

    Teams that hold a ``CheckRunner`` for the same command call ``outcome`` with its (memoized)
    result, so the final patch is never checked twice. ``run`` is the plain Harness protocol:
    it runs the command in the workspace it is given.
    """

    command: str
    name: str = "check"
    model: Optional[str] = None
    supports_output_schema = False

    @staticmethod
    def verdict(result: CheckResult) -> Verdict:
        feedback = f"The check {result.describe()}"
        if not result.passed:
            feedback += "\nMake the check pass without weakening it."
        return Verdict(score=10.0 if result.passed else 0.0, passed=result.passed, feedback=feedback)

    def outcome(self, result: CheckResult, role: str) -> tuple[Verdict, HarnessRun]:
        """The verdict for a check result, and the (zero-cost) run that stands for it."""
        verdict = self.verdict(result)
        run = HarnessRun(
            self.name,
            None,
            role,
            verdict.model_dump_json(),
            0,
            Usage(cost_usd=0.0),
            result.duration_s,
            log=result.output_tail,
        )
        return verdict, run

    async def run(
        self,
        workspace: Workspace,
        prompt: str,
        *,
        role: str = "reviewer",
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HarnessRun:
        return self.outcome(await execute_check(workspace, self.command, timeout), role)[1]

"""The task's own test suite as a benchmark team's ``check`` reviewer or judge.

A team spec with ``reviewer=check`` or ``judge=check`` gets, per task, a check that runs the task's
rebuild and test commands (``rebuild_cmds`` / ``test_cmds`` / ``print_cmds``: whole-suite commands
that name no hidden test) on the visible repository, **without** the hidden test patch, in a clean
sandbox of the instance image (no secrets, as in grading), and parses the log with the task's own
parser. Exit codes are not used: many ``test_cmds`` pipe through ``tee``.

A patch passes iff it applies and introduces no failure against the base commit: every test that
passed there still passes (a test that no longer runs, e.g. because the build broke, counts as
failing) and no new test fails. Tests that already failed at the base commit are ignored. The base
commit's results are computed once per task and shared by every team in the run; the first check
runs them alongside the candidate.

Agents see the check's summary and the tail of its log: the same output running the project's tests
themselves would give. They never see the hidden tests, ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` or the
grader's output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.teams.harnesses.check import TAIL_CHARS, Check, CheckResult, tail
from agent.teams.types import Workspace

from .dataset import BenchTask
from .evaluate import GradingError, _apply_results, build_eval_script, run_log_parser, run_script, split_log
from .sandbox import SandboxBackend

SUITE_CHECK = "the project's test suite (no new failures against the base commit)"
CHECK_TIMEOUT_S = 3600.0
MAX_NAMES = 20
PATCH_PATH = "/tmp/bench-pred.diff"


@dataclass
class SuiteRun:
    status: dict[str, str]
    log: str
    applied: bool
    duration_s: float
    timed_out: bool = False


def _passed(status: str) -> bool:
    return "pass" in status.lower()


def _failed(status: str) -> bool:
    return "fail" in status.lower()


def compare(base: dict[str, str], now: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(tests that passed at the base commit and now fail, ... that no longer run, new tests that fail)."""
    regressed = sorted(t for t, s in base.items() if _passed(s) and t in now and not _passed(now[t]))
    vanished = sorted(t for t, s in base.items() if _passed(s) and t not in now)
    new_failures = sorted(t for t, s in now.items() if t not in base and _failed(s))
    return regressed, vanished, new_failures


def _names(label: str, tests: list[str]) -> str:
    shown = ", ".join(tests[:MAX_NAMES])
    more = f" (and {len(tests) - MAX_NAMES} more)" if len(tests) > MAX_NAMES else ""
    return f"{label}: {shown}{more}\n"


def summarize(base: SuiteRun, now: SuiteRun) -> tuple[bool, str]:
    """The verdict and the summary shown to the agents."""
    if not now.applied:
        return False, "The patch did not apply to a clean checkout of the base commit.\n"
    regressed, vanished, new_failures = compare(base.status, now.status)
    counts: dict[str, int] = {}
    for s in now.status.values():
        key = "passed" if _passed(s) else "failed" if _failed(s) else "skipped"
        counts[key] = counts.get(key, 0) + 1
    text = (
        f"{len(now.status)} tests ran: {counts.get('passed', 0)} passed, {counts.get('failed', 0)} "
        f"failed, {counts.get('skipped', 0)} skipped. Against the base commit: {len(regressed)} "
        f"newly failing, {len(vanished)} no longer run, {len(new_failures)} new tests failing.\n"
    )
    if regressed:
        text += _names("Passed at the base commit, now fail", regressed)
    if vanished:
        text += _names("Passed at the base commit, no longer run (does it still build?)", vanished)
    if new_failures:
        text += _names("New tests that fail", new_failures)
    return not (regressed or vanished or new_failures), text


class SuiteChecks:
    """Builds the per-task suite check and caches each task's base-commit results."""

    def __init__(self, backend: SandboxBackend, *, timeout_s: float = CHECK_TIMEOUT_S) -> None:
        self.backend = backend
        self.timeout_s = timeout_s
        self._baselines: dict[str, asyncio.Future[SuiteRun]] = {}

    def for_task(self, task: BenchTask) -> Check:
        async def evaluate(workspace: Workspace, patch: str) -> CheckResult:
            return await self.check(task, patch)

        return Check(SUITE_CHECK, self.timeout_s, evaluate=evaluate)

    async def check(self, task: BenchTask, patch: str) -> CheckResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        base, now = await asyncio.gather(self.baseline(task), self.run(task, patch))
        duration = loop.time() - started
        if base.timed_out:
            raise GradingError(f"the base commit's test suite timed out after {self.timeout_s:g}s")
        if not base.status:
            raise GradingError("the base commit's test suite reported no test results")
        if now.timed_out:
            text = f"[the test suite timed out after {self.timeout_s:g}s]"
            return CheckResult(SUITE_CHECK, False, None, text, duration)
        passed, summary = summarize(base, now)
        room = max(TAIL_CHARS - len(summary), 1000)
        output = summary + "\nLog (tail):\n" + tail(now.log, room)
        return CheckResult(SUITE_CHECK, passed, 0 if passed else 1, output, duration)

    async def baseline(self, task: BenchTask) -> SuiteRun:
        """The base commit's results, run once per task; a failed run is retried by the next call."""
        job = self._baselines.get(task.task_id)
        if job is None or (job.done() and (job.cancelled() or job.exception() is not None)):
            job = asyncio.ensure_future(self.run(task, ""))
            self._baselines[task.task_id] = job
        # Shielded: one team's cancellation must not cancel the run other teams wait for.
        return await asyncio.shield(job)

    async def run(self, task: BenchTask, patch: str) -> SuiteRun:
        """The visible test suite with ``patch`` applied (none for the base commit)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        files = {PATCH_PATH: patch} if patch.strip() else {}
        script = build_eval_script(
            task.grading, test_patch_path=None, patch_path=PATCH_PATH if files else None
        )
        result = await run_script(task, files, script, backend=self.backend, timeout_s=self.timeout_s)
        if result is None:
            return SuiteRun({}, "", True, loop.time() - started, timed_out=True)
        head, log = split_log(result)
        applied = _apply_results(head).get("patch", False) if files else True
        status = await asyncio.to_thread(run_log_parser, task.grading.log_parser, log)
        return SuiteRun(status, log, applied, loop.time() - started)

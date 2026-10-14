"""Team topologies: solo, coder + reviewer loop, best-of-N with a judge.

The loops are the ``agent.workflows`` patterns (``RefineLoop``, ``BestOfN``) driven through a
small runner: a "draft" is a patch, a "generator" is a harness working in a workspace and an
"evaluator" is a reviewer/judge harness working in a *fork* with the candidate patch applied, so
evaluation can never alter the worker's workspace.

Every team returns all harness runs (roles ``worker``, ``coder``, ``reviewer``, ``judge``),
including failed ones, so cost is never under-reported.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from agent.workflows import (
    BestOfN,
    CheckpointDecision,
    RefineCheckpoint,
    RefineLoop,
    Verdict,
    VerdictLike,
)

from .harnesses.check import Check, CheckHarness, CheckResult, CheckRunner
from .types import CodingTask, Harness, HarnessRun, TeamResult, Workspace
from .verdict import VerdictOutcome, request_verdict
from .worktree import close_to_completion as _close

MAX_PATCH_IN_PROMPT = 40_000

WORKER_PROMPT = (
    "Solve the task below by editing the files in the current repository (the current "
    "directory). Run the project's tests where they exist to verify your change. Do not commit.\n\n"
    "Task:\n{instruction}"
)

REVIEW_PROMPT = (
    "You are the {role} for a candidate change to the repository in the current directory. The "
    "candidate is applied as uncommitted modifications: changes to existing files show in "
    "`git diff HEAD`, new files are untracked (see `git status`). Read it, and run the "
    "project's tests or write throwaway checks where useful. Do NOT modify, create or delete "
    "any file in the repository: you are judging the change, not fixing it. "
    "Judge only against the task below: does the change correctly and completely solve it, "
    "without breaking anything else?\n\nTask:\n{instruction}\n{check}\nCandidate patch:\n```diff\n"
    "{patch}\n```"
)

REVISION_PROMPT = (
    "{query}\n\n--- Review of your previous attempt ---\nScore: {score}/10\n{feedback}\n\n"
    "Your previous changes are already in the working tree. Address every point of the "
    "feedback, keep what already works, and verify with the tests again."
)


def harness_label(harness: Harness) -> str:
    return f"{harness.name}@{harness.model}" if harness.model else harness.name


async def _run_harness(
    harness: Harness,
    workspace: Workspace,
    prompt: str,
    *,
    role: str,
    session_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> tuple[Optional[HarnessRun], Optional[str]]:
    """One harness run; an exception or non-zero exit becomes an error string (never a raise)."""
    try:
        run = await harness.run(workspace, prompt, role=role, session_id=session_id, timeout=timeout)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if run.exit_code != 0:
        return run, f"exit code {run.exit_code}: {run.final_message[:300]}"
    return run, None


async def _review_in_fork(
    harness: Harness,
    workspace: Workspace,
    task: CodingTask,
    patch: str,
    *,
    role: str,
    timeout: Optional[float],
    check: Optional[CheckResult] = None,
) -> VerdictOutcome:
    """Judge ``patch`` in a throwaway fork of ``workspace`` with the patch applied.

    ``check`` (the objective check's result for this patch) is shown to the reviewer.
    """
    shown = patch
    if len(shown) > MAX_PATCH_IN_PROMPT:
        shown = shown[:MAX_PATCH_IN_PROMPT] + "\n... [patch truncated; use git diff for the rest]"
    check_text = f"\nObjective check result for this patch: {check.describe()}\n" if check else ""
    prompt = REVIEW_PROMPT.format(role=role, instruction=task.instruction, check=check_text, patch=shown)
    fork: Optional[Workspace] = None
    try:
        fork = await workspace.fork()
        await fork.apply(patch)
        return await request_verdict(harness, fork, prompt, role=role, timeout=timeout)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return VerdictOutcome(
            Verdict(score=0.0, passed=False, feedback=f"The {role} could not run ({error})."), None, error
        )
    finally:
        if fork is not None:
            await _close(fork)


async def _evaluate(
    harness: Harness,
    checker: Optional[CheckRunner],
    workspace: Workspace,
    task: CodingTask,
    patch: str,
    *,
    role: str,
    timeout: Optional[float],
    show_check: bool,
) -> tuple[VerdictOutcome, Optional[CheckResult]]:
    """A reviewer's or judge's verdict on ``patch`` and, with a ``checker``, the check result.

    A check harness is answered from the checker's (memoized) result, so the command runs once per
    patch however many roles ask. An LLM reviewer (``show_check``) is shown the check result; an
    LLM judge is not, its check runs alongside it.
    """
    if isinstance(harness, CheckHarness):
        assert checker is not None  # the teams create one for a check reviewer/judge
        result = await checker(patch)
        verdict, run = harness.outcome(result, role)
        return VerdictOutcome(verdict, run), result
    if checker is None:
        return await _review_in_fork(harness, workspace, task, patch, role=role, timeout=timeout), None
    if show_check:
        result = await checker(patch)
        return (
            await _review_in_fork(harness, workspace, task, patch, role=role, timeout=timeout, check=result),
            result,
        )
    outcome, result = await asyncio.gather(
        _review_in_fork(harness, workspace, task, patch, role=role, timeout=timeout), checker(patch)
    )
    return outcome, result


def _own_check(harness: Harness, check: Optional[Check], timeout: Optional[float]) -> Optional[Check]:
    """The team's check; a check reviewer/judge implies one with its command."""
    if not isinstance(harness, CheckHarness):
        return check
    if check is not None and check.command != harness.command:
        raise ValueError("a check reviewer/judge and the team's check must run the same command")
    return check or Check(harness.command, timeout)


class _Draft:
    """A patch; ``str(draft)`` is what the workflow patterns hand to the evaluator."""

    def __init__(self, patch: str, *, unchanged: bool = False) -> None:
        self.patch = patch
        self.unchanged = unchanged

    def __str__(self) -> str:
        return self.patch


async def _call(agent: Any, text: str) -> Any:
    return await agent(text)


def _failure(**fields: Any) -> dict[str, Any]:
    return fields


async def _record_check(meta: dict[str, Any], checker: Optional[CheckRunner], patch: str) -> None:
    """Run the team's objective check on the final patch and record it in ``meta["check"]``."""
    if checker is not None:
        meta["check"] = (await checker(patch)).to_dict()


@dataclass
class SoloTeam:
    """One harness solves the task in the given workspace."""

    worker: Harness
    name: str = ""
    timeout: Optional[float] = None
    check: Optional[Check] = None

    def __post_init__(self) -> None:
        self.name = self.name or f"solo:worker={harness_label(self.worker)}"

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        run, error = await _run_harness(
            self.worker,
            workspace,
            WORKER_PROMPT.format(instruction=task.instruction),
            role="worker",
            timeout=self.timeout,
        )
        patch = await workspace.diff()
        meta: dict[str, Any] = {"team": self.name, "worker": harness_label(self.worker)}
        if error:
            meta["failures"] = [_failure(role="worker", error=error)]
        await _record_check(meta, self.check.bind(workspace) if self.check else None, patch)
        return TeamResult(patch, [run] if run else [], time.perf_counter() - start, meta)


@dataclass
class _Coder:
    harness: Harness
    workspace: Workspace
    timeout: Optional[float]
    name: str = "coder"
    runs: list[HarnessRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    session_id: Optional[str] = None
    last_patch: Optional[str] = None

    async def __call__(self, prompt: str) -> _Draft:
        run, error = await _run_harness(
            self.harness,
            self.workspace,
            prompt,
            role="coder",
            session_id=self.session_id,
            timeout=self.timeout,
        )
        if run is not None:
            self.runs.append(run)
            self.session_id = run.session_id or self.session_id
        round_no = len(self.log) + 1
        if error:
            self.failures.append(_failure(round=round_no, role="coder", error=error))
        patch = await self.workspace.diff()
        draft = _Draft(patch, unchanged=patch == self.last_patch)
        self.last_patch = patch
        self.log.append({"round": round_no, "changed": not draft.unchanged, "coder_error": error})
        return draft


@dataclass
class _Reviewer:
    harness: Harness
    workspace: Workspace
    task: CodingTask
    timeout: Optional[float]
    name: str = "reviewer"
    runs: list[HarnessRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    calls: int = 0
    checker: Optional[CheckRunner] = None

    async def __call__(self, patch: str) -> Verdict:
        self.calls += 1
        if not patch.strip():
            return Verdict(
                score=0.0, passed=False, feedback="The change is empty: no file was modified."
            )
        if patch in self.verdicts:  # a stuck coder re-submitted the same state; do not pay again
            return self.verdicts[patch]
        outcome, _ = await _evaluate(
            self.harness,
            self.checker,
            self.workspace,
            self.task,
            patch,
            role="reviewer",
            timeout=self.timeout,
            show_check=True,
        )
        if outcome.run is not None:
            self.runs.append(outcome.run)
        if outcome.error:
            self.failures.append(_failure(round=self.calls, role="reviewer", error=outcome.error))
        self.verdicts[patch] = outcome.verdict
        return outcome.verdict


@dataclass
class ReviewTeam:
    """A coder works in the workspace; a reviewer judges each attempt in a fork; the coder revises.

    The coder keeps its session across rounds when its harness supports resume. Stops when the
    reviewer passes the change, after ``rounds`` attempts, or when the coder stops changing
    anything. Returns the best-scoring patch (earliest on ties), not necessarily the last; with a
    ``check`` reviewer, which can only pass or fail, the last attempt when none passes.
    A failed review scores 0 and is recorded in ``meta["failures"]``. A ``check`` reviewer implies
    the team's check (same command); an LLM reviewer is shown the check result for each patch.
    """

    coder: Harness
    reviewer: Harness
    rounds: int = 3
    name: str = ""
    timeout: Optional[float] = None
    check: Optional[Check] = None

    def __post_init__(self) -> None:
        if self.rounds < 1:
            raise ValueError("rounds must be >= 1")
        self.check = _own_check(self.reviewer, self.check, self.timeout)
        self.name = self.name or (
            f"review:coder={harness_label(self.coder)},reviewer={harness_label(self.reviewer)},"
            f"rounds={self.rounds}"
        )

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        runs: list[HarnessRun] = []  # chronological, shared by both agents
        checker = self.check.bind(workspace) if self.check else None
        coder = _Coder(self.coder, workspace, self.timeout, runs=runs)
        reviewer = _Reviewer(self.reviewer, workspace, task, self.timeout, runs=runs, checker=checker)

        async def stop_when_stuck(state: RefineCheckpoint) -> CheckpointDecision:
            return CheckpointDecision(continue_refining=not state.draft.unchanged)

        def revision(query: str, draft: Any, verdict: VerdictLike) -> str:
            return REVISION_PROMPT.format(query=query, score=verdict.score, feedback=verdict.feedback)

        loop = RefineLoop(
            generator=coder,
            evaluator=reviewer,
            max_iterations=self.rounds,
            runner=_call,
            revision_prompt=revision,
            checkpoint=stop_when_stuck,
        )
        flow = await loop.run(WORKER_PROMPT.format(instruction=task.instruction))
        scores: list[float] = flow.meta["scores"]
        for entry, score in zip(coder.log, scores):
            entry["score"] = score
        meta: dict[str, Any] = {
            "team": self.name,
            "rounds": flow.meta["iterations"],
            "scores": scores,
            "accepted": flow.meta["accepted"],
            "best_round": scores.index(max(scores)) + 1,
            "round_log": coder.log,
        }
        failures = coder.failures + reviewer.failures
        if failures:
            meta["failures"] = failures
        patch = flow.final_output.patch
        if isinstance(self.reviewer, CheckHarness) and coder.last_patch and coder.last_patch.strip():
            # A pass/fail reviewer cannot rank attempts that all fail: the last one has had the
            # most feedback, so return it rather than attempt 1.
            patch = coder.last_patch
            meta["best_round"] = len(scores)
        await _record_check(meta, checker, patch)
        return TeamResult(patch, runs, time.perf_counter() - start, meta)


class WorkerFailed(RuntimeError):
    pass


@dataclass
class _Candidate:
    index: int
    harness: Harness
    patch: str = ""
    runs: list[HarnessRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    check: Optional[CheckResult] = None

    def __str__(self) -> str:
        return self.patch


_current: contextvars.ContextVar[_Candidate] = contextvars.ContextVar("current_candidate")


@dataclass
class _Worker:
    candidate: _Candidate
    workspace: Workspace
    timeout: Optional[float]

    @property
    def name(self) -> str:
        return f"worker-{self.candidate.index}"

    async def __call__(self, prompt: str) -> _Candidate:
        c = self.candidate
        _current.set(c)  # BestOfN runs a candidate's generate and evaluate in one task
        fork = await self.workspace.fork()
        try:
            run, error = await _run_harness(c.harness, fork, prompt, role="worker", timeout=self.timeout)
            if run is not None:
                c.runs.append(run)
            c.patch = await fork.diff()
        finally:
            await _close(fork)
        if error:
            c.failures.append(_failure(role="worker", error=error))
            if not c.patch.strip():
                raise WorkerFailed(error)
        return c


@dataclass
class _Judge:
    harness: Harness
    workspace: Workspace
    task: CodingTask
    timeout: Optional[float]
    name: str = "judge"
    checker: Optional[CheckRunner] = None

    async def __call__(self, patch: str) -> Verdict:
        c = _current.get()
        if not patch.strip():
            return Verdict(
                score=0.0, passed=False, feedback="The change is empty: no file was modified."
            )
        outcome, result = await _evaluate(
            self.harness,
            self.checker,
            self.workspace,
            self.task,
            patch,
            role="judge",
            timeout=self.timeout,
            show_check=False,
        )
        if result is not None:
            c.check = result
        if outcome.run is not None:
            c.runs.append(outcome.run)
        if outcome.error:
            c.failures.append(_failure(role="judge", error=outcome.error))
        return outcome.verdict


@dataclass
class BestTeam:
    """Each worker solves the task in its own fork; the judge scores every candidate patch.

    Workers may be different harnesses. A worker that crashes without producing a change is
    dropped; if all fail the result has an empty patch. Ties pick the earliest candidate.
    The judge scores each candidate independently (0-10), it does not rank them side by side.
    With a ``check`` (or a ``check`` judge), every candidate is also checked and candidates that
    fail rank below every candidate that passes; scores order within each group.
    """

    workers: Sequence[Harness]
    judge: Harness
    name: str = ""
    timeout: Optional[float] = None
    max_concurrency: Optional[int] = None
    check: Optional[Check] = None

    def __post_init__(self) -> None:
        if len(self.workers) < 1:
            raise ValueError("BestTeam needs at least one worker")
        self.check = _own_check(self.judge, self.check, self.timeout)
        self.name = self.name or (
            f"best:workers={'+'.join(harness_label(w) for w in self.workers)},"
            f"judge={harness_label(self.judge)}"
        )

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        candidates = [_Candidate(i, h) for i, h in enumerate(self.workers, 1)]
        checker = self.check.bind(workspace) if self.check else None
        gate = checker is not None
        pattern = BestOfN(
            generator=[_Worker(c, workspace, self.timeout) for c in candidates],
            evaluator=_Judge(self.judge, workspace, task, self.timeout, checker=checker),
            runner=_call,
            max_concurrency=self.max_concurrency or len(candidates),
        )
        patch = ""
        flow_failures: list[dict[str, Any]] = []
        meta: dict[str, Any] = {"team": self.name}
        try:
            flow = await pattern.run(WORKER_PROMPT.format(instruction=task.instruction))
            patch = flow.final_output.patch
            flow_failures = flow.meta.get("failures", [])
            meta.update(chosen=flow.meta["chosen"], scores=flow.meta["scores"])
            if gate:
                scored = [c for c in candidates if flow.meta["scores"][c.index - 1] is not None]
                best = max(
                    scored,
                    key=lambda c: (c.check is not None and c.check.passed, flow.meta["scores"][c.index - 1]),
                )
                patch, meta["chosen"] = best.patch, best.index
        except RuntimeError as exc:
            meta.update(chosen=None, scores=[None] * len(candidates), error=str(exc))
        runs: list[HarnessRun] = []
        entries = []
        for c in candidates:
            entry = {
                "candidate": c.index,
                "harness": harness_label(c.harness),
                "score": meta["scores"][c.index - 1],
                "runs": list(range(len(runs), len(runs) + len(c.runs))),
            }
            if gate:
                entry["check"] = c.check.to_dict() if c.check else None
            entries.append(entry)
            runs += c.runs
        meta["candidates"] = entries
        failures = [{"candidate": c.index, **f} for c in candidates for f in c.failures]
        # Candidates that failed outside their harness (e.g. fork() raising) only appear in the
        # pattern's own failure list.
        seen = {f["candidate"] for f in failures}
        failures += [
            {"role": "worker", **f} for f in flow_failures if f["candidate"] not in seen
        ]
        if failures:
            meta["failures"] = failures
        await _record_check(meta, checker, patch)
        return TeamResult(patch, runs, time.perf_counter() - start, meta)

"""Team topologies: solo, coder + reviewer loop, best-of-N with a judge.

The loops are the ``agent.workflows`` patterns (``RefineLoop``, ``BestOfN``) driven through a
small runner: a "draft" is a patch, a "generator" is a harness working in a workspace and an
"evaluator" is a reviewer/judge harness working in a *fork* with the candidate patch applied, so
evaluation can never alter the worker's workspace.

Every team returns all harness runs (roles ``worker``, ``coder``, ``reviewer``, ``judge``),
including failed ones, so cost is never under-reported.
"""

from __future__ import annotations

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

from .types import CodingTask, Harness, HarnessRun, TeamResult, Workspace
from .verdict import VerdictOutcome, request_verdict

MAX_PATCH_IN_PROMPT = 40_000

WORKER_PROMPT = (
    "Solve the task below by editing the files in the current repository (the current "
    "directory). Run the project's tests where they exist to verify your change. Do not commit.\n\n"
    "Task:\n{instruction}"
)

REVIEW_PROMPT = (
    "You are the {role} for a candidate change to the repository in the current directory. The "
    "candidate is applied as uncommitted modifications (see `git status` and `git diff`). Read "
    "it, and run the project's tests or write throwaway checks where useful. Do NOT modify, "
    "create or delete any file in the repository: you are judging the change, not fixing it. "
    "Judge only against the task below: does the change correctly and completely solve it, "
    "without breaking anything else?\n\nTask:\n{instruction}\n\nCandidate patch:\n```diff\n"
    "{patch}\n```"
)

REVISION_PROMPT = (
    "{query}\n\n--- Review of your previous attempt ---\nScore: {score}/10\n{feedback}\n\n"
    "Your previous changes are already in the working tree. Address every point of the "
    "feedback, keep what already works, and verify with the tests again."
)


def harness_label(harness: Harness) -> str:
    return f"{harness.name}@{harness.model}" if harness.model else harness.name


async def _close(workspace: Workspace) -> None:
    try:
        await workspace.close()
    except Exception:
        pass


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
) -> VerdictOutcome:
    """Judge ``patch`` in a throwaway fork of ``workspace`` with the patch applied."""
    shown = patch
    if len(shown) > MAX_PATCH_IN_PROMPT:
        shown = shown[:MAX_PATCH_IN_PROMPT] + "\n... [patch truncated; use git diff for the rest]"
    prompt = REVIEW_PROMPT.format(role=role, instruction=task.instruction, patch=shown)
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


@dataclass
class SoloTeam:
    """One harness solves the task in the given workspace."""

    worker: Harness
    name: str = ""
    timeout: Optional[float] = None

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

    async def __call__(self, patch: str) -> Verdict:
        self.calls += 1
        if not patch.strip():
            return Verdict(
                score=0.0, passed=False, feedback="The change is empty: no file was modified."
            )
        if patch in self.verdicts:  # a stuck coder re-submitted the same state; do not pay again
            return self.verdicts[patch]
        outcome = await _review_in_fork(
            self.harness, self.workspace, self.task, patch, role="reviewer", timeout=self.timeout
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
    anything. Returns the best-scoring patch (earliest on ties), not necessarily the last.
    A failed review scores 0 and is recorded in ``meta["failures"]``.
    """

    coder: Harness
    reviewer: Harness
    rounds: int = 3
    name: str = ""
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        if self.rounds < 1:
            raise ValueError("rounds must be >= 1")
        self.name = self.name or (
            f"review:coder={harness_label(self.coder)},reviewer={harness_label(self.reviewer)},"
            f"rounds={self.rounds}"
        )

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        runs: list[HarnessRun] = []  # chronological, shared by both agents
        coder = _Coder(self.coder, workspace, self.timeout, runs=runs)
        reviewer = _Reviewer(self.reviewer, workspace, task, self.timeout, runs=runs)

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
        return TeamResult(flow.final_output.patch, runs, time.perf_counter() - start, meta)


class WorkerFailed(RuntimeError):
    pass


@dataclass
class _Candidate:
    index: int
    harness: Harness
    patch: str = ""
    runs: list[HarnessRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

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

    async def __call__(self, patch: str) -> Verdict:
        c = _current.get()
        if not patch.strip():
            return Verdict(
                score=0.0, passed=False, feedback="The change is empty: no file was modified."
            )
        outcome = await _review_in_fork(
            self.harness, self.workspace, self.task, patch, role="judge", timeout=self.timeout
        )
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
    """

    workers: Sequence[Harness]
    judge: Harness
    name: str = ""
    timeout: Optional[float] = None
    max_concurrency: Optional[int] = None

    def __post_init__(self) -> None:
        if len(self.workers) < 1:
            raise ValueError("BestTeam needs at least one worker")
        self.name = self.name or (
            f"best:workers={'+'.join(harness_label(w) for w in self.workers)},"
            f"judge={harness_label(self.judge)}"
        )

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        candidates = [_Candidate(i, h) for i, h in enumerate(self.workers, 1)]
        pattern = BestOfN(
            generator=[_Worker(c, workspace, self.timeout) for c in candidates],
            evaluator=_Judge(self.judge, workspace, task, self.timeout),
            runner=_call,
            max_concurrency=self.max_concurrency or len(candidates),
        )
        patch = ""
        meta: dict[str, Any] = {"team": self.name}
        try:
            flow = await pattern.run(WORKER_PROMPT.format(instruction=task.instruction))
            patch = flow.final_output.patch
            meta.update(chosen=flow.meta["chosen"], scores=flow.meta["scores"])
        except RuntimeError as exc:
            meta.update(chosen=None, scores=[None] * len(candidates), error=str(exc))
        runs: list[HarnessRun] = []
        entries = []
        for c in candidates:
            entries.append(
                {
                    "candidate": c.index,
                    "harness": harness_label(c.harness),
                    "score": meta["scores"][c.index - 1],
                    "runs": list(range(len(runs), len(runs) + len(c.runs))),
                }
            )
            runs += c.runs
        meta["candidates"] = entries
        failures = [{"candidate": c.index, **f} for c in candidates for f in c.failures]
        if failures:
            meta["failures"] = failures
        return TeamResult(patch, runs, time.perf_counter() - start, meta)

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .base import AgentRunner, StepRecord, VerdictLike, WorkflowResult, agent_name, default_runner


@dataclass
class BestOfN:
    """Fan out N candidate generations concurrently, score each with the evaluator, keep the best.

    ``variant_prompt`` lets each candidate get a different input (e.g. a different angle or
    genre) so the candidates are diverse rather than N samples of the same prompt.
    ``max_concurrency`` bounds concurrent candidates. Failed candidates are recorded and
    skipped; the run fails only if none produces a valid verdict.
    """

    generator: Any
    evaluator: Any
    n: int = 3
    runner: AgentRunner = field(default=default_runner)
    variant_prompt: Optional[Callable[[str, int], str]] = None
    max_concurrency: int = 3

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError("n must be >= 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")

    async def _candidate(
        self, query: str, idx: int, steps: list[StepRecord]
    ) -> tuple[Any, VerdictLike]:
        gen_input = self.variant_prompt(query, idx) if self.variant_prompt else query
        steps.append(StepRecord("generate", agent_name(self.generator), gen_input, None, iteration=idx))
        try:
            draft = await self.runner(self.generator, gen_input)
            steps[-1].output = draft
            steps.append(
                StepRecord("evaluate", agent_name(self.evaluator), str(draft), None, iteration=idx)
            )
            verdict = await self.runner(self.evaluator, str(draft))
            if not isinstance(verdict, VerdictLike):
                raise TypeError(
                    f"Evaluator {agent_name(self.evaluator)!r} must return an object with "
                    f"score/passed/feedback (e.g. output_type=Verdict), got {type(verdict).__name__}"
                )
            steps[-1].output = verdict
            return draft, verdict
        except Exception as exc:
            steps[-1].output = {"error": f"{type(exc).__name__}: {exc}"}
            raise

    async def run(self, query: str) -> WorkflowResult:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        candidate_steps: list[list[StepRecord]] = [[] for _ in range(self.n)]

        async def candidate(idx: int) -> tuple[Any, VerdictLike]:
            async with semaphore:
                return await self._candidate(query, idx, candidate_steps[idx - 1])

        candidates = await asyncio.gather(
            *(candidate(i) for i in range(1, self.n + 1)), return_exceptions=True
        )
        result = WorkflowResult(final_output=None)
        successes = []
        failures = []
        scores = []
        for idx, (steps, output) in enumerate(zip(candidate_steps, candidates), 1):
            result.steps.extend(steps)
            if isinstance(output, Exception):
                failures.append({"candidate": idx, "error": f"{type(output).__name__}: {output}"})
                scores.append(None)
            elif isinstance(output, BaseException):
                raise output
            else:
                draft, verdict = output
                successes.append((idx, draft, verdict))
                scores.append(verdict.score)
        if not successes:
            message = f"All {self.n} candidates failed: {failures}"
            error = candidates[0]
            if isinstance(error, TypeError):
                raise TypeError(message) from error
            raise RuntimeError(message) from error
        best_idx, result.final_output, result.verdict = max(successes, key=lambda c: c[2].score)
        result.meta.update(
            chosen=best_idx,
            scores=scores,
        )
        if failures:
            result.meta["failures"] = failures
        return result

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
    """

    generator: Any
    evaluator: Any
    n: int = 3
    runner: AgentRunner = field(default=default_runner)
    variant_prompt: Optional[Callable[[str, int], str]] = None

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError("n must be >= 1")

    async def _candidate(self, query: str, idx: int) -> tuple[list[StepRecord], Any, VerdictLike]:
        gen_input = self.variant_prompt(query, idx) if self.variant_prompt else query
        draft = await self.runner(self.generator, gen_input)
        verdict = await self.runner(self.evaluator, str(draft))
        steps = [
            StepRecord("generate", agent_name(self.generator), gen_input, draft, iteration=idx),
            StepRecord("evaluate", agent_name(self.evaluator), str(draft), verdict, iteration=idx),
        ]
        return steps, draft, verdict

    async def run(self, query: str) -> WorkflowResult:
        candidates = await asyncio.gather(*(self._candidate(query, i) for i in range(1, self.n + 1)))
        result = WorkflowResult(final_output=None)
        for steps, _, _ in candidates:
            result.steps.extend(steps)
        best_idx = max(range(len(candidates)), key=lambda k: candidates[k][2].score)
        _, result.final_output, result.verdict = candidates[best_idx]
        result.meta.update(
            chosen=best_idx + 1,
            scores=[c[2].score for c in candidates],
        )
        return result

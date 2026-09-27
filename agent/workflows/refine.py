from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .base import AgentRunner, StepRecord, VerdictLike, WorkflowResult, agent_name, default_runner


def default_revision_prompt(query: str, draft: Any, verdict: VerdictLike) -> str:
    return (
        f"Original request:\n{query}\n\n"
        f"Your previous draft:\n{draft}\n\n"
        f"Reviewer score: {verdict.score}/10\n"
        f"Reviewer feedback:\n{verdict.feedback}\n\n"
        "Revise the draft to address every point of feedback while keeping what already works. "
        "Return only the full revised draft."
    )


@dataclass
class RefineLoop:
    """Evaluator-optimizer loop: generate, critique, revise until the evaluator is satisfied.

    Stops when the verdict passes (``passed`` is true, or ``score >= threshold`` when a threshold
    is set) or after ``max_iterations`` drafts. The best-scoring draft is returned, so a late
    regression never replaces an earlier, better draft.
    """

    generator: Any
    evaluator: Any
    max_iterations: int = 3
    threshold: Optional[float] = None
    runner: AgentRunner = field(default=default_runner)
    revision_prompt: Callable[[str, Any, VerdictLike], str] = default_revision_prompt
    on_iteration: Optional[Callable[[int, Any, VerdictLike], None]] = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")

    def is_accepted(self, verdict: VerdictLike) -> bool:
        if self.threshold is not None:
            return verdict.score >= self.threshold
        return bool(verdict.passed)

    async def run(self, query: str) -> WorkflowResult:
        result = WorkflowResult(final_output=None)
        best: Optional[tuple[Any, VerdictLike]] = None
        gen_input = query

        for i in range(1, self.max_iterations + 1):
            draft = await self.runner(self.generator, gen_input)
            result.steps.append(
                StepRecord("generate", agent_name(self.generator), gen_input, draft, iteration=i)
            )

            verdict = await self.runner(self.evaluator, str(draft))
            if not isinstance(verdict, VerdictLike):
                raise TypeError(
                    f"Evaluator {agent_name(self.evaluator)!r} must return an object with "
                    f"score/passed/feedback (e.g. output_type=Verdict), got {type(verdict).__name__}"
                )
            result.steps.append(
                StepRecord("evaluate", agent_name(self.evaluator), str(draft), verdict, iteration=i)
            )
            if self.on_iteration:
                self.on_iteration(i, draft, verdict)

            if best is None or verdict.score > best[1].score:
                best = (draft, verdict)
            if self.is_accepted(verdict):
                break
            gen_input = self.revision_prompt(query, draft, verdict)

        assert best is not None
        result.final_output, result.verdict = best
        result.meta.update(
            iterations=i,
            accepted=self.is_accepted(best[1]),
            scores=[s.output.score for s in result.steps if s.name == "evaluate"],
        )
        return result

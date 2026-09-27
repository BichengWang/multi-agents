"""Reusable orchestration patterns built on run_agent."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agents import Agent

from . import runner
from .schemas import Evaluation


async def pipeline(steps: Sequence[Agent[Any]], input: str) -> list[Any]:
    """Run agents in sequence, feeding each final output to the next. Returns every output."""
    outputs: list[Any] = []
    current = input
    for step in steps:
        result = await runner.run_agent(step, current)
        outputs.append(result.final_output)
        current = str(result.final_output)
    return outputs


async def fan_out(agent: Agent[Any], inputs: Sequence[str], *, concurrency: int = 5) -> list[Any]:
    """Run one agent over many inputs in parallel. Failed runs yield None, order is preserved."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(item: str) -> Any:
        async with semaphore:
            try:
                return (await runner.run_agent(agent, item)).final_output
            except Exception:
                return None

    return list(await asyncio.gather(*(_one(item) for item in inputs)))


@dataclass
class RefineRound:
    draft: str
    evaluation: Evaluation


@dataclass
class RefineResult:
    output: str
    evaluation: Evaluation
    rounds: list[RefineRound] = field(default_factory=list)


def _evaluator_input(task: str, draft: str) -> str:
    return f"Task:\n{task}\n\nDraft to evaluate:\n{draft}"


def _revision_input(task: str, draft: str, evaluation: Evaluation) -> str:
    issues = "\n".join(f"- {issue}" for issue in evaluation.issues) or "- (none listed)"
    return (
        f"Task:\n{task}\n\n"
        f"Your previous draft (scored {evaluation.score}/10):\n{draft}\n\n"
        f"Issues to fix:\n{issues}\n\n"
        f"Reviewer suggestions:\n{evaluation.suggestions}\n\n"
        "Rewrite the draft addressing every issue. Return only the revised draft."
    )


async def refine_loop(
    generator: Agent[Any],
    evaluator: Agent[Any],
    task: str,
    *,
    max_rounds: int = 3,
    threshold: int = 8,
) -> RefineResult:
    """Generate, evaluate and revise until the evaluator passes the draft or rounds run out.

    The evaluator must use ``output_type=Evaluation``. Returns the best-scoring draft.
    """
    if max_rounds < 1:
        raise ValueError("max_rounds must be at least 1")

    rounds: list[RefineRound] = []
    generator_input = task
    for _ in range(max_rounds):
        draft = str((await runner.run_agent(generator, generator_input)).final_output)
        evaluation = (
            await runner.run_agent(evaluator, _evaluator_input(task, draft))
        ).final_output_as(Evaluation)
        rounds.append(RefineRound(draft=draft, evaluation=evaluation))
        if evaluation.passed and evaluation.score >= threshold:
            break
        generator_input = _revision_input(task, draft, evaluation)

    # Latest round wins ties, since it has seen the most feedback.
    best = max(reversed(rounds), key=lambda r: r.evaluation.score)
    return RefineResult(output=best.draft, evaluation=best.evaluation, rounds=rounds)

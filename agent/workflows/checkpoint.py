from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .base import VerdictLike


@dataclass(frozen=True)
class RefineCheckpoint:
    """The evaluated draft a human can inspect before the next refinement iteration."""

    query: str
    iteration: int
    draft: Any
    verdict: VerdictLike


@dataclass(frozen=True)
class CheckpointDecision:
    """Continue with optional steering feedback, or stop and return the best draft so far."""

    continue_refining: bool = True
    feedback: str = ""


CheckpointHandler = Callable[[RefineCheckpoint], Awaitable[CheckpointDecision]]


async def cli_checkpoint(state: RefineCheckpoint) -> CheckpointDecision:
    """Prompt between revisions without blocking the event loop; EOF stops refinement."""
    print(f"\nDraft after iteration {state.iteration}:\n{state.draft}")
    print(f"\nReviewer score: {state.verdict.score}/10\nFeedback:\n{state.verdict.feedback}")
    try:
        answer = await asyncio.to_thread(
            input,
            "\nEnter to revise, /stop to finish with the best draft, or type steering feedback: ",
        )
    except EOFError:
        print("\nInput closed; finishing with the best draft.")
        return CheckpointDecision(continue_refining=False)
    answer = answer.strip()
    if answer.lower() == "/stop":
        return CheckpointDecision(continue_refining=False)
    return CheckpointDecision(feedback=answer)

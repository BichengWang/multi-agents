from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.workflows import Verdict


@dataclass
class FakeAgent:
    """Stand-in for an SDK Agent: `respond` maps the input text to the final output."""

    name: str
    respond: Callable[[str], Any]
    calls: list[str] = field(default_factory=list)


async def fake_runner(agent: FakeAgent, input_text: str) -> Any:
    agent.calls.append(input_text)
    return agent.respond(input_text)


@dataclass
class ConcurrentFakeRunner:
    in_flight: int = 0
    max_in_flight: int = 0

    async def __call__(self, agent: FakeAgent, input_text: str) -> Any:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            return await fake_runner(agent, input_text)
        finally:
            self.in_flight -= 1


def scripted_evaluator(scores: list[float], pass_at: float = 8.0) -> FakeAgent:
    """Evaluator that returns the given scores in order (last one repeats)."""
    it = iter(scores)
    last = [scores[-1]]

    def respond(_: str) -> Verdict:
        score = next(it, last[0])
        return Verdict(score=score, passed=score >= pass_at, feedback=f"feedback for {score}")

    return FakeAgent("Evaluator", respond)


def counting_generator(prefix: str = "draft") -> FakeAgent:
    agent = FakeAgent(prefix, lambda _: None)
    agent.respond = lambda _: f"{prefix}-{len(agent.calls)}"
    return agent

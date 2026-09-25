from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# A runner takes an agent and a text input and returns the agent's final output.
# Injecting it keeps the workflow patterns SDK-agnostic and makes them testable offline.
AgentRunner = Callable[[Any, str], Awaitable[Any]]


async def default_runner(agent: Any, input_text: str) -> Any:
    """Run an OpenAI Agents SDK agent and return its final output."""
    from agents import Runner

    result = await Runner.run(agent, input_text)
    return result.final_output


class Verdict(BaseModel):
    """Base structured output for evaluator agents used by the refine / best-of-n patterns."""

    score: float = Field(description="Overall quality score from 1 to 10.")
    passed: bool = Field(description="True if the output is good enough to ship without revision.")
    feedback: str = Field(description="Concrete, actionable feedback for improving the output.")


@runtime_checkable
class VerdictLike(Protocol):
    score: float
    passed: bool
    feedback: str


def agent_name(agent: Any) -> str:
    return getattr(agent, "name", None) or type(agent).__name__


@dataclass
class StepRecord:
    """One agent invocation inside a workflow."""

    name: str
    agent: str
    input: str
    output: Any
    iteration: Optional[int] = None


@dataclass
class WorkflowResult:
    """Final output of a workflow plus the full trace of steps that produced it."""

    final_output: Any
    steps: list[StepRecord] = field(default_factory=list)
    verdict: Optional[VerdictLike] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def outputs(self, name: str) -> list[Any]:
        return [s.output for s in self.steps if s.name == name]

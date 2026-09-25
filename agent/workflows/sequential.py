from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .base import AgentRunner, StepRecord, WorkflowResult, agent_name, default_runner


@dataclass
class Step:
    """A pipeline stage. `prepare` builds the agent input from (original query, previous output)."""

    name: str
    agent: Any
    prepare: Optional[Callable[[str, Any], str]] = None


@dataclass
class SequentialWorkflow:
    """Pipe each agent's output into the next agent (A -> B -> C)."""

    steps: list[Step]
    runner: AgentRunner = field(default=default_runner)

    async def run(self, query: str) -> WorkflowResult:
        result = WorkflowResult(final_output=None)
        previous: Any = query
        for step in self.steps:
            input_text = step.prepare(query, previous) if step.prepare else str(previous)
            output = await self.runner(step.agent, input_text)
            result.steps.append(StepRecord(step.name, agent_name(step.agent), input_text, output))
            previous = output
        result.final_output = previous
        return result

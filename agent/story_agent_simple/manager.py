from __future__ import annotations

from typing import Any, Literal

from agent.workflows import (
    AgentRunner,
    BestOfN,
    RefineLoop,
    SequentialWorkflow,
    Step,
    WorkflowResult,
    default_runner,
)

from .my_agents.generator_agent import generator_agent
from .my_agents.evaluator_agent import evaluator_agent

Mode = Literal["single", "refine", "best-of-n"]

VARIANT_ANGLES = [
    "a character-driven take",
    "a plot-twist-driven take",
    "an unconventional structure or point of view",
    "a genre-blending take",
    "a minimalist, emotionally intimate take",
]


def _variant_prompt(query: str, idx: int) -> str:
    angle = VARIANT_ANGLES[(idx - 1) % len(VARIANT_ANGLES)]
    return f"{query}\n\nApproach this as {angle}."


class SimpleStoryManager:
    """
    Orchestrates the simple story workflow: generation and evaluation of story concepts.

    Modes:
      - single:    generate once, evaluate once
      - refine:    generate -> evaluate -> revise until the evaluator accepts (or max iterations)
      - best-of-n: generate N diverse candidates in parallel, keep the highest-scoring one
    """

    def __init__(
        self,
        mode: Mode = "refine",
        max_iterations: int = 3,
        threshold: float | None = None,
        n: int = 3,
        runner: AgentRunner = default_runner,
        generator: Any = generator_agent,
        evaluator: Any = evaluator_agent,
    ):
        self.mode = mode
        self.max_iterations = max_iterations
        self.threshold = threshold
        self.n = n
        self.runner = runner
        self.generator = generator
        self.evaluator = evaluator

    def build_workflow(self):
        if self.mode == "single":
            return SequentialWorkflow(
                [Step("generate", self.generator), Step("evaluate", self.evaluator)],
                runner=self.runner,
            )
        if self.mode == "refine":
            return RefineLoop(
                self.generator,
                self.evaluator,
                max_iterations=self.max_iterations,
                threshold=self.threshold,
                runner=self.runner,
                on_iteration=lambda i, _draft, v: print(
                    f"  iteration {i}: score={v.score} passed={v.passed}"
                ),
            )
        if self.mode == "best-of-n":
            return BestOfN(
                self.generator,
                self.evaluator,
                n=self.n,
                runner=self.runner,
                variant_prompt=_variant_prompt,
            )
        raise ValueError(f"Unknown mode: {self.mode!r}")

    async def run(self, query: str) -> WorkflowResult:
        print(f"Starting simple story agent workflow (mode={self.mode})...")
        result = await self.build_workflow().run(query)

        if self.mode == "single":
            story, evaluation = result.steps[0].output, result.final_output
        else:
            story, evaluation = result.final_output, result.verdict
        print(f"\nGenerated Story:\n{story}")
        print(f"\nEvaluation:\n{_format_evaluation(evaluation)}")
        if result.meta:
            print(f"\nRun info: {result.meta}")

        print("\nSimple story agent workflow complete.")
        return result


def _format_evaluation(evaluation: Any) -> str:
    if hasattr(evaluation, "model_dump"):
        data = evaluation.model_dump()
        feedback = data.pop("feedback", "")
        lines = [f"{k}: {v}" for k, v in data.items()]
        return "\n".join(lines + ["feedback:", str(feedback)])
    return str(evaluation)

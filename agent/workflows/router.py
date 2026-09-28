from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from .base import AgentRunner, StepRecord, WorkflowResult, agent_name, default_runner

# A route handler runs the specialist workflow for a query. It may return a WorkflowResult
# (its steps are merged into the router's trace) or any plain output.
RouteHandler = Callable[[str], Awaitable[Any]]


class RouteDecision(BaseModel):
    """Structured output for the classifier agent that drives a Router."""

    route: str = Field(description="Name of the route that should handle the request.")
    confidence: float = Field(description="Confidence in the choice, from 0 to 1.")
    reason: str = Field(description="One sentence explaining the choice.")


@dataclass
class Route:
    name: str
    description: str
    handler: RouteHandler


def default_classifier_prompt(query: str, routes: list[Route]) -> str:
    options = "\n".join(f"- {r.name}: {r.description}" for r in routes)
    return (
        f"Available routes:\n{options}\n\n"
        f"Request:\n{query}\n\n"
        "Pick exactly one route name from the list above."
    )


@dataclass
class Router:
    """Triage pattern: a classifier agent picks one specialist workflow to handle the query.

    If the classifier names an unknown route, or its confidence is below ``min_confidence``, the
    ``fallback`` route is used; without a fallback that is an error. ``force`` skips the classifier.
    """

    classifier: Any
    routes: list[Route]
    runner: AgentRunner = field(default=default_runner)
    fallback: Optional[str] = None
    min_confidence: float = 0.0
    classifier_prompt: Callable[[str, list[Route]], str] = default_classifier_prompt

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("Router needs at least one route")
        names = [r.name for r in self.routes]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate route names: {names}")
        self._by_name = {r.name: r for r in self.routes}
        if self.fallback is not None and self.fallback not in self._by_name:
            raise ValueError(f"Unknown fallback route {self.fallback!r}")

    def _resolve(self, decision: Any) -> tuple[Route, bool]:
        route = self._by_name.get(str(decision.route).strip())
        if route is not None and decision.confidence >= self.min_confidence:
            return route, False
        if self.fallback is None:
            raise ValueError(
                f"Classifier chose {decision.route!r} (confidence {decision.confidence}); "
                f"expected one of {sorted(self._by_name)} with confidence >= {self.min_confidence}"
            )
        return self._by_name[self.fallback], True

    async def run(self, query: str, force: Optional[str] = None) -> WorkflowResult:
        result = WorkflowResult(final_output=None)

        if force is not None:
            if force not in self._by_name:
                raise ValueError(f"Unknown route {force!r}; expected one of {sorted(self._by_name)}")
            route, used_fallback = self._by_name[force], False
            result.meta.update(route=force, forced=True)
        else:
            prompt = self.classifier_prompt(query, self.routes)
            decision = await self.runner(self.classifier, prompt)
            if not all(hasattr(decision, a) for a in ("route", "confidence", "reason")):
                raise TypeError(
                    f"Classifier {agent_name(self.classifier)!r} must return route/confidence/reason "
                    f"(e.g. output_type=RouteDecision), got {type(decision).__name__}"
                )
            result.steps.append(StepRecord("classify", agent_name(self.classifier), prompt, decision))
            route, used_fallback = self._resolve(decision)
            result.meta.update(
                route=route.name,
                classified_as=decision.route,
                confidence=decision.confidence,
                reason=decision.reason,
                fallback_used=used_fallback,
            )

        output = await route.handler(query)
        if isinstance(output, WorkflowResult):
            result.steps.extend(output.steps)
            result.verdict = output.verdict
            result.final_output = output.final_output
            result.meta["route_meta"] = output.meta
        else:
            result.final_output = output
        return result

from __future__ import annotations

from typing import Any, Optional

from agent.workflows import AgentRunner, CheckpointHandler, Route, Router, WorkflowResult, default_runner

from .classifier_agent import classifier_agent


def build_routes(
    runner: AgentRunner = default_runner,
    *,
    story_checkpoint: Optional[CheckpointHandler] = None,
) -> list[Route]:
    """Specialist workflows the triage agent can dispatch to (imported lazily per route)."""

    async def story(query: str) -> Any:
        from agent.story_agent_simple.manager import SimpleStoryManager

        return await SimpleStoryManager(runner=runner, checkpoint=story_checkpoint).run(query)

    async def store(query: str) -> Any:
        from agent.store_agent.manager import StoreAgentManager

        return await StoreAgentManager(runner=runner).run(query)

    async def financial(query: str) -> Any:
        # Uses the SDK directly (streaming + tools), so it ignores the injected runner.
        from agent.financial_research_agent.manager import FinancialResearchManager

        return await FinancialResearchManager().run(query)

    return [
        Route(
            "story",
            "Creative writing: invent, draft, or improve a story, plot, or narrative concept.",
            story,
        ),
        Route(
            "store",
            "Business ideas: design a store, shop, or small-business concept and a plan to run it.",
            store,
        ),
        Route(
            "financial",
            "Financial research: analyze a public company, stock, earnings, or market question.",
            financial,
        ),
    ]


class TriageManager:
    """Single entrypoint: classify the request, then run the matching specialist workflow."""

    def __init__(
        self,
        runner: AgentRunner = default_runner,
        routes: Optional[list[Route]] = None,
        classifier: Any = classifier_agent,
        fallback: Optional[str] = None,
        min_confidence: float = 0.5,
        story_checkpoint: Optional[CheckpointHandler] = None,
    ):
        self.router = Router(
            classifier,
            routes if routes is not None else build_routes(runner, story_checkpoint=story_checkpoint),
            runner=runner,
            fallback=fallback,
            min_confidence=min_confidence,
        )

    async def run(self, query: str, route: Optional[str] = None) -> WorkflowResult:
        if route is None:
            print("Classifying request...")
        result = await self.router.run(query, force=route)
        meta = result.meta
        if not meta.get("forced"):
            note = " (fallback)" if meta["fallback_used"] else ""
            print(
                f"Routed to '{meta['route']}'{note}: classified as '{meta['classified_as']}' "
                f"with confidence {meta['confidence']:.2f} - {meta['reason']}"
            )
        return result

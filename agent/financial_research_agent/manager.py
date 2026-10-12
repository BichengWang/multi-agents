from __future__ import annotations

from typing import Any

from rich.console import Console

from agents import function_tool

from agent.workflows import AgentRunner, StepRecord, WorkflowResult, default_runner, map_concurrently
from agent.workflows.base import agent_name

from .agents.financials_agent import financials_agent
from .agents.planner_agent import FinancialSearchItem, planner_agent
from .agents.risk_agent import risk_agent
from .agents.search_agent import search_agent
from .agents.verifier_agent import verifier_agent
from .agents.writer_agent import writer_agent
from .printer import Printer


class FinancialResearchManager:
    """Plan, search, write with optional analyst tools, and verify through one runner."""

    def __init__(
        self,
        runner: AgentRunner = default_runner,
        *,
        max_concurrency: int = 3,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.runner = runner
        self.max_concurrency = max_concurrency

    async def _call(
        self, result: WorkflowResult, name: str, agent: Any, input_text: str
    ) -> Any:
        output = await self.runner(agent, input_text)
        result.steps.append(StepRecord(name, agent_name(agent), input_text, output))
        return output

    async def run(self, query: str) -> WorkflowResult:
        # State is per run: a reusable manager must not mix traces or tools between requests.
        result = WorkflowResult(final_output=None)
        printer = Printer(Console())
        try:
            printer.update_item("planning", "Planning searches...")
            plan = await self._call(result, "plan", planner_agent, f"Query: {query}")
            printer.update_item(
                "planning", f"Will perform {len(plan.searches)} searches", is_done=True
            )
            printer.update_item("searching", "Searching...")
            completed = 0

            async def search(item: FinancialSearchItem) -> StepRecord:
                nonlocal completed
                input_text = f"Search term: {item.query}\nReason: {item.reason}"
                output = await self.runner(search_agent, input_text)
                completed += 1
                printer.update_item(
                    "searching", f"Searching... {completed}/{len(plan.searches)} completed"
                )
                return StepRecord("search", agent_name(search_agent), input_text, output)

            searches = await map_concurrently(
                plan.searches, search, max_concurrency=self.max_concurrency
            )
            result.steps.extend(searches)
            printer.mark_item_done("searching")

            @function_tool(failure_error_function=None)
            async def fundamentals_analysis(input: str) -> str:
                """Get a short write-up of key financial metrics from research evidence."""
                analysis = await self._call(result, "fundamentals", financials_agent, input)
                return str(analysis.summary)

            @function_tool(failure_error_function=None)
            async def risk_analysis(input: str) -> str:
                """Get a short write-up of potential red flags from research evidence."""
                analysis = await self._call(result, "risk", risk_agent, input)
                return str(analysis.summary)

            writer = writer_agent.clone(tools=[fundamentals_analysis, risk_analysis])
            printer.update_item("writing", "Writing report...")
            input_text = (
                f"Original query: {query}\n"
                f"Summarized search results: {[str(step.output) for step in searches]}"
            )
            report = await self._call(result, "write", writer, input_text)
            printer.mark_item_done("writing")
            printer.update_item("verifying", "Verifying report...")
            verification = await self._call(
                result, "verify", verifier_agent, report.markdown_report
            )
            printer.mark_item_done("verifying")
            result.final_output = report
            result.meta.update(searches=len(searches), verification=verification)
        finally:
            printer.end()

        print(f"\n\n=====REPORT SUMMARY=====\n\n{report.short_summary}")
        print(f"\n\n=====REPORT=====\n\n{report.markdown_report}")
        print("\n\n=====FOLLOW UP QUESTIONS=====\n\n" + "\n".join(report.follow_up_questions))
        print(f"\n\n=====VERIFICATION=====\n\n{verification}")
        return result

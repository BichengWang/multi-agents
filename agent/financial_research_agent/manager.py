from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from rich.console import Console

from agents import FunctionTool, custom_span, function_tool, gen_trace_id, trace

from agent.workflows import AgentRunner, StepRecord, WorkflowResult, default_runner
from agent.workflows.base import agent_name

from .agents.financials_agent import financials_agent
from .agents.planner_agent import FinancialSearchItem, FinancialSearchPlan, planner_agent
from .agents.risk_agent import risk_agent
from .agents.search_agent import search_agent
from .agents.verifier_agent import VerificationResult, verifier_agent
from .agents.writer_agent import FinancialReportData, writer_agent
from .printer import Printer


class FinancialResearchManager:
    """
    Orchestrates the full flow: planning, searching, sub‑analysis, writing, and verification.
    """

    def __init__(
        self, runner: AgentRunner = default_runner, *, show_progress: bool | None = None
    ) -> None:
        self.runner = runner
        self.console = Console()
        self.show_progress = self.console.is_terminal if show_progress is None else show_progress
        self.printer: Printer | None = None

    def _update_item(self, item_id: str, content: str, **kwargs: Any) -> None:
        if self.printer is not None:
            self.printer.update_item(item_id, content, **kwargs)

    def _mark_item_done(self, item_id: str) -> None:
        if self.printer is not None:
            self.printer.mark_item_done(item_id)

    async def _run_agent(
        self, name: str, agent: Any, input_text: str, steps: list[StepRecord]
    ) -> Any:
        step = StepRecord(name, agent_name(agent), input_text, None)
        steps.append(step)
        try:
            step.output = await self.runner(agent, input_text)
            return step.output
        except Exception as exc:
            step.output = {"error": f"{type(exc).__name__}: {exc}"}
            raise

    async def run(self, query: str) -> WorkflowResult:
        result = WorkflowResult(final_output=None)
        trace_id = gen_trace_id()
        if self.show_progress:
            self.printer = Printer(self.console)
        try:
            with trace("Financial research trace", trace_id=trace_id):
                self._update_item(
                    "trace_id",
                    f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}",
                    is_done=True,
                    hide_checkmark=True,
                )
                self._update_item("start", "Starting financial research...", is_done=True)
                search_plan = await self._plan_searches(query, result.steps)
                search_results = await self._perform_searches(search_plan, result.steps)
                report = await self._write_report(query, search_results, result.steps)
                verification = await self._verify_report(report, result.steps)
                result.final_output = report.markdown_report

                final_report = f"Report summary\n\n{report.short_summary}"
                self._update_item("final_report", final_report, is_done=True)
        finally:
            if self.printer is not None:
                self.printer.end()
                self.printer = None

        # Print to stdout
        print("\n\n=====REPORT=====\n\n")
        print(f"Report:\n{report.markdown_report}")
        print("\n\n=====FOLLOW UP QUESTIONS=====\n\n")
        print("\n".join(report.follow_up_questions))
        print("\n\n=====VERIFICATION=====\n\n")
        print(verification)
        return result

    async def _plan_searches(self, query: str, steps: list[StepRecord]) -> FinancialSearchPlan:
        self._update_item("planning", "Planning searches...")
        plan = await self._run_agent("plan", planner_agent, f"Query: {query}", steps)
        self._update_item(
            "planning",
            f"Will perform {len(plan.searches)} searches",
            is_done=True,
        )
        return plan

    async def _perform_searches(
        self, search_plan: FinancialSearchPlan, steps: list[StepRecord]
    ) -> Sequence[str]:
        with custom_span("Search the web"):
            self._update_item("searching", "Searching...")
            tasks = [asyncio.create_task(self._search(item, steps)) for item in search_plan.searches]
            results: list[str] = []
            num_completed = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                if result is not None:
                    results.append(result)
                num_completed += 1
                self._update_item(
                    "searching", f"Searching... {num_completed}/{len(tasks)} completed"
                )
            self._mark_item_done("searching")
            return results

    async def _search(self, item: FinancialSearchItem, steps: list[StepRecord]) -> str | None:
        input_data = f"Search term: {item.query}\nReason: {item.reason}"
        try:
            output = await self._run_agent("search", search_agent, input_data, steps)
            return str(output)
        except Exception:
            return None

    def _analysis_tool(
        self, name: str, agent: Any, description: str, steps: list[StepRecord]
    ) -> FunctionTool:
        @function_tool(name_override=name, description_override=description)
        async def analyze(input: str) -> str:
            output = await self._run_agent(name, agent, input, steps)
            return str(output.summary)

        return analyze

    async def _write_report(
        self, query: str, search_results: Sequence[str], steps: list[StepRecord]
    ) -> FinancialReportData:
        # Expose the specialist analysts as tools so the writer can invoke them inline
        # and still produce the final FinancialReportData output.
        fundamentals_tool = self._analysis_tool(
            "fundamentals_analysis", financials_agent,
            "Use to get a short write‑up of key financial metrics", steps,
        )
        risk_tool = self._analysis_tool(
            "risk_analysis", risk_agent,
            "Use to get a short write‑up of potential red flags", steps,
        )
        writer_with_tools = writer_agent.clone(tools=[fundamentals_tool, risk_tool])
        self._update_item("writing", "Thinking about report...")
        input_data = f"Original query: {query}\nSummarized search results: {search_results}"
        report = await self._run_agent("write", writer_with_tools, input_data, steps)
        self._mark_item_done("writing")
        return report

    async def _verify_report(
        self, report: FinancialReportData, steps: list[StepRecord]
    ) -> VerificationResult:
        self._update_item("verifying", "Verifying report...")
        verification = await self._run_agent("verify", verifier_agent, report.markdown_report, steps)
        self._mark_item_done("verifying")
        return verification

import asyncio
import json

from contextlib import nullcontext

import pytest

pytest.importorskip("agents")

from agents.tool_context import ToolContext  # noqa: E402

from agent.financial_research_agent.agents.financials_agent import AnalysisSummary  # noqa: E402
from agent.financial_research_agent.agents.planner_agent import (  # noqa: E402
    FinancialSearchItem,
    FinancialSearchPlan,
)
from agent.financial_research_agent.agents.risk_agent import AnalysisSummary as RiskSummary  # noqa: E402
from agent.financial_research_agent.agents.verifier_agent import VerificationResult  # noqa: E402
from agent.financial_research_agent.agents.writer_agent import FinancialReportData  # noqa: E402
from agent.financial_research_agent.manager import FinancialResearchManager  # noqa: E402
from agent.triage.classifier_agent import classifier_agent  # noqa: E402
from agent.workflows import RouteDecision, RunTracker  # noqa: E402


PLAN = FinancialSearchPlan(searches=[
    FinancialSearchItem(query="annual filing", reason="reported financials"),
    FinancialSearchItem(query="risk disclosure", reason="material risks"),
])
REPORT = FinancialReportData(
    short_summary="Revenue grew, but concentration remains a risk.",
    markdown_report="# Company\nRevenue rose 12%. Customer concentration needs further review.",
    follow_up_questions=["How much revenue comes from the largest customer?"],
)
VERIFICATION = VerificationResult(verified=False, issues="Customer concentration is unquantified.")


async def invoke_analysis(tool):
    arguments = json.dumps({"input": "Reported revenue rose 12%; concentration is unquantified."})
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id=f"call_{tool.name}",
        tool_arguments=arguments,
    )
    return await tool.on_invoke_tool(context, arguments)


async def financial_runner(agent, input_text):
    if agent is classifier_agent:
        return RouteDecision(route="financial", confidence=0.9, reason="company analysis")
    if agent.name == "FinancialPlannerAgent":
        return PLAN
    if agent.name == "FinancialSearchAgent":
        return "Reported revenue rose 12%." if "annual filing" in input_text else "Concentration risk."
    if agent.name == "FundamentalsAnalystAgent":
        return AnalysisSummary(summary="Revenue growth: 12%.")
    if agent.name == "RiskAnalystAgent":
        return RiskSummary(summary="Customer concentration needs quantification.")
    if agent.name == "FinancialWriterAgent":
        await asyncio.gather(*(invoke_analysis(tool) for tool in agent.tools))
        return REPORT
    if agent.name == "VerificationAgent":
        return VERIFICATION
    raise AssertionError(f"Unexpected agent: {agent.name}")


@pytest.mark.parametrize("forced", [False, True])
def test_shared_cli_saves_financial_report_verification_and_nested_analysis(monkeypatch, tmp_path, forced):
    import agent.main as entrypoint

    tracker = RunTracker(base=financial_runner)
    monkeypatch.setattr(entrypoint, "RunTracker", lambda: tracker)
    monkeypatch.setattr("agents.trace", lambda _: nullcontext())
    args = ["agent", "company", "--runs-dir", str(tmp_path)]
    if forced:
        args += ["--route", "financial"]
    monkeypatch.setattr("sys.argv", args)
    asyncio.run(entrypoint.main())

    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["final_output"] == REPORT.model_dump()
    assert record["meta"]["route_meta"]["verification"] == VERIFICATION.model_dump()
    assert record["steps"][-1]["input"] == REPORT.markdown_report
    assert record["steps"][-1]["output"]["verified"] is False
    analyses = {step["name"]: step["output"] for step in record["steps"] if step["name"] in {"risk", "fundamentals"}}
    assert analyses == {
        "fundamentals": {"summary": "Revenue growth: 12%."},
        "risk": {"summary": "Customer concentration needs quantification."},
    }
    assert record["totals"]["calls"] == (7 if forced else 8)
    assert record["totals"]["errors"] == 0


def test_failed_search_aborts_report_and_waits_for_other_searches():
    async def scenario():
        sibling_started = asyncio.Event()
        sibling_cleaned = asyncio.Event()

        async def runner(agent, input_text):
            if agent.name == "FinancialPlannerAgent":
                return PLAN
            if agent.name == "FinancialSearchAgent":
                if "annual filing" in input_text:
                    await sibling_started.wait()
                    raise RuntimeError("filing unavailable")
                sibling_started.set()
                try:
                    await asyncio.Future()
                finally:
                    await asyncio.sleep(0)
                    sibling_cleaned.set()
            raise AssertionError("A report must not be synthesized after search failure")

        with pytest.raises(RuntimeError, match="filing unavailable"):
            await FinancialResearchManager(runner=runner, max_concurrency=2).run("company")
        assert sibling_cleaned.is_set()

    asyncio.run(scenario())


def test_analyst_failure_propagates_instead_of_becoming_report_evidence():
    async def runner(agent, input_text):
        if agent.name == "FundamentalsAnalystAgent":
            raise RuntimeError("analysis unavailable")
        return await financial_runner(agent, input_text)

    with pytest.raises(RuntimeError, match="analysis unavailable"):
        asyncio.run(FinancialResearchManager(runner=runner).run("company"))

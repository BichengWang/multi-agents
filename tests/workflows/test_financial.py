import asyncio
import json
from collections import Counter
from contextlib import nullcontext

import pytest

pytest.importorskip("agents")

from agents.tool_context import ToolContext  # noqa: E402
from rich.console import Console  # noqa: E402

from agent.financial_research_agent import manager as financial  # noqa: E402
from agent.financial_research_agent.agents.financials_agent import AnalysisSummary  # noqa: E402
from agent.financial_research_agent.agents.planner_agent import FinancialSearchItem, FinancialSearchPlan  # noqa: E402
from agent.financial_research_agent.agents.risk_agent import AnalysisSummary as RiskSummary  # noqa: E402
from agent.financial_research_agent.agents.verifier_agent import VerificationResult  # noqa: E402
from agent.financial_research_agent.agents.writer_agent import FinancialReportData  # noqa: E402
from agent.triage.manager import TriageManager  # noqa: E402
from agent.workflows import RouteDecision, RunTracker  # noqa: E402

from .fakes import FakeAgent, fake_runner  # noqa: E402


@pytest.fixture
def financial_fakes(monkeypatch):
    report = FinancialReportData(
        short_summary="Revenue grew, with supply risks to watch.",
        markdown_report="# Company report\n\nRevenue grew 10%. Supply risks remain.",
        follow_up_questions=["Will revenue growth continue?"],
    )
    fakes = {
        "TriageClassifierAgent": FakeAgent(
            "TriageClassifierAgent",
            lambda _: RouteDecision(route="financial", confidence=0.9, reason="company analysis"),
        ),
        "FinancialPlannerAgent": FakeAgent(
            "FinancialPlannerAgent",
            lambda _: FinancialSearchPlan(searches=[
                FinancialSearchItem(query="company earnings", reason="financial metrics"),
                FinancialSearchItem(query="company risks", reason="risk factors"),
            ]),
        ),
        "FinancialSearchAgent": FakeAgent("FinancialSearchAgent", lambda text: f"Summary: {text}"),
        "FundamentalsAnalystAgent": FakeAgent(
            "FundamentalsAnalystAgent", lambda _: AnalysisSummary(summary="Revenue grew 10%."),
        ),
        "RiskAnalystAgent": FakeAgent(
            "RiskAnalystAgent", lambda _: RiskSummary(summary="Supply risks remain."),
        ),
        "FinancialWriterAgent": FakeAgent("FinancialWriterAgent", lambda _: report),
        "VerificationAgent": FakeAgent(
            "VerificationAgent", lambda _: VerificationResult(verified=True, issues=""),
        ),
    }

    async def runner(agent, input_text):
        if agent.name == "FinancialWriterAgent":
            assert [tool.name for tool in agent.tools] == ["fundamentals_analysis", "risk_analysis"]
            summaries = await asyncio.gather(*(
                tool.on_invoke_tool(
                    ToolContext(
                        context=None, tool_name=tool.name, tool_call_id=tool.name,
                        tool_arguments=json.dumps({"input": input_text}),
                    ),
                    json.dumps({"input": input_text}),
                )
                for tool in agent.tools
            ))
            assert summaries == ["Revenue grew 10%.", "Supply risks remain."]
        return await fake_runner(fakes[agent.name], input_text)

    def unexpected_sdk_call(*_args, **_kwargs):
        pytest.fail("Financial workflow bypassed the injected runner")

    monkeypatch.setattr("agents.Runner.run", unexpected_sdk_call)
    monkeypatch.setattr("agents.Runner.run_streamed", unexpected_sdk_call)
    monkeypatch.setattr("agents.trace", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(financial, "trace", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(financial, "custom_span", lambda *_args, **_kwargs: nullcontext())
    return runner, fakes, report


@pytest.mark.parametrize("forced", [False, True])
def test_shared_cli_saves_financial_report_and_every_call(monkeypatch, tmp_path, financial_fakes, forced):
    import agent.main as entrypoint

    runner, fakes, report = financial_fakes
    tracker = RunTracker(base=runner)
    monkeypatch.setattr(entrypoint, "RunTracker", lambda: tracker)
    monkeypatch.setattr(financial, "Console", lambda: Console(force_terminal=False))
    monkeypatch.setattr(financial, "Printer", lambda _: pytest.fail("Headless run started a Live UI"))
    args = ["agent", "Analyze the company", "--runs-dir", str(tmp_path)]
    if forced:
        args += ["--route", "financial"]
    monkeypatch.setattr("sys.argv", args)

    asyncio.run(entrypoint.main())

    paths = list(tmp_path.glob("*.json"))
    assert len(paths) == 1
    record = json.loads(paths[0].read_text())
    assert record["workflow"] == record["meta"]["route"] == "financial"
    assert record["final_output"] == report.markdown_report
    expected_calls = Counter(
        (fake.name, input_text) for fake in fakes.values() for input_text in fake.calls
    )
    assert Counter((step["agent"], step["input"]) for step in record["steps"]) == expected_calls
    assert Counter(call["agent"] for call in record["calls"]) == Counter(
        step["agent"] for step in record["steps"]
    )
    assert record["totals"]["calls"] == len(record["steps"]) == (7 if forced else 8)
    assert record["totals"]["errors"] == 0
    assert [step["name"] for step in record["steps"]] == (
        ([] if forced else ["classify"])
        + ["plan", "search", "search", "write", "fundamentals_analysis", "risk_analysis", "verify"]
    )
    assert record["steps"][-1]["output"] == {"verified": True, "issues": ""}
    assert fakes["VerificationAgent"].calls == [report.markdown_report]
    writer_input = fakes["FinancialWriterAgent"].calls[0]
    assert "company earnings" in writer_input and "company risks" in writer_input


def test_financial_triage_records_failed_search_and_finishes(financial_fakes):
    runner, fakes, report = financial_fakes

    def search(text):
        if "company earnings" in text:
            raise RuntimeError("search unavailable")
        return "Remaining search summary"

    fakes["FinancialSearchAgent"].respond = search
    tracker = RunTracker(base=runner)
    result = asyncio.run(TriageManager(runner=tracker).run("Analyze the company", route="financial"))

    assert result.final_output == report.markdown_report
    assert len(result.steps) == len(tracker.calls) == 7
    assert result.outputs("search") == [
        {"error": "RuntimeError: search unavailable"}, "Remaining search summary",
    ]
    assert tracker.totals()["errors"] == 1
    assert "Remaining search summary" in fakes["FinancialWriterAgent"].calls[0]
    assert "search unavailable" not in fakes["FinancialWriterAgent"].calls[0]


@pytest.mark.parametrize(
    "interactive,show_progress,enabled",
    [(False, None, False), (True, None, True), (False, True, True), (True, False, False)],
)
@pytest.mark.parametrize("fail", [False, True])
def test_financial_progress_is_optional_and_stops_on_exit(
    monkeypatch, financial_fakes, interactive, show_progress, enabled, fail,
):
    runner, fakes, report = financial_fakes
    events = []
    monkeypatch.setattr(financial, "Console", lambda: Console(force_terminal=interactive))
    monkeypatch.setattr("agent.financial_research_agent.printer.Live.start", lambda _: events.append("start"))
    monkeypatch.setattr("agent.financial_research_agent.printer.Live.stop", lambda _: events.append("stop"))
    manager = financial.FinancialResearchManager(runner=runner, show_progress=show_progress)
    if fail:
        def unavailable(_):
            raise RuntimeError("planner unavailable")

        fakes["FinancialPlannerAgent"].respond = unavailable
        with pytest.raises(RuntimeError, match="planner unavailable"):
            asyncio.run(manager.run("Analyze the company"))
    else:
        result = asyncio.run(manager.run("Analyze the company"))
        assert result.final_output == report.markdown_report
    assert events == (["start", "stop"] if enabled else [])
    assert manager.printer is None

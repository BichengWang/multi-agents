import json
from pathlib import Path

import pytest

from agent.teams import ClaudeHarness, CodexHarness, ExecResult
from agent.teams.harnesses.claude import parse_claude_output
from agent.teams.harnesses.codex import parse_codex_events
from agent.teams.pricing import cost_usd
from agent.teams.verdict import VerdictParseError, parse_verdict

from .fakes import ScriptedWorkspace, run

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


# ---- Claude -------------------------------------------------------------------------------


def test_claude_success_result_session_cost_and_tokens():
    parsed = parse_claude_output(fixture("claude_success.json"))
    assert parsed.ok and parsed.text.startswith("I fixed the failing test")
    assert parsed.session_id == json.loads(fixture("claude_success.json"))["session_id"]
    assert parsed.model == "claude-haiku-5-5"
    # modelUsage: inputTokens + cacheRead + cacheCreation; cached = cache reads.
    raw = json.loads(fixture("claude_success.json"))["modelUsage"]["claude-haiku-5-5"]
    assert parsed.usage.input_tokens == (
        raw["inputTokens"] + raw["cacheReadInputTokens"] + raw["cacheCreationInputTokens"]
    )
    assert parsed.usage.cached_input_tokens == raw["cacheReadInputTokens"]
    assert parsed.usage.output_tokens == raw["outputTokens"]
    assert parsed.usage.cost_usd == pytest.approx(0.01055866)


def test_claude_error_result_is_not_ok_and_keeps_tokens_from_model_usage():
    parsed = parse_claude_output(fixture("claude_error.json"))
    assert not parsed.ok
    assert "maximum budget" in parsed.text
    assert parsed.usage.input_tokens > 0  # top-level `usage` is zeroed in this result
    assert parsed.usage.cost_usd == pytest.approx(0.00598522)


def test_claude_missing_cost_is_unpriced_not_zero():
    data = json.loads(fixture("claude_success.json"))
    del data["total_cost_usd"]
    assert parse_claude_output(json.dumps(data)).usage.cost_usd is None


def test_claude_structured_output_survives_as_json_text():
    parsed = parse_claude_output(fixture("claude_verdict.json"))
    verdict = parse_verdict(parsed.text)
    assert 0 <= verdict.score <= 10 and isinstance(verdict.passed, bool) and verdict.feedback


def test_claude_harness_turns_failures_into_failed_runs_not_exceptions():
    ws = ScriptedWorkspace(lambda argv, stdin: ExecResult(1, fixture("claude_error.json"), ""))
    result = run(ClaudeHarness("haiku").run(ws, "task"))
    assert result.exit_code == 1 and "maximum budget" in result.final_message
    assert result.usage.cost_usd == pytest.approx(0.00598522)

    ws = ScriptedWorkspace(lambda argv, stdin: ExecResult(0, "not json at all", ""))
    result = run(ClaudeHarness().run(ws, "task"))
    assert result.exit_code != 0 and "JSON" in result.final_message

    def boom(argv, stdin):
        raise TimeoutError()

    result = run(ClaudeHarness().run(ScriptedWorkspace(boom), "task", timeout=5))
    assert result.exit_code == 124


# ---- Codex --------------------------------------------------------------------------------


def test_codex_events_final_message_thread_usage_and_log():
    parsed = parse_codex_events(fixture("codex_success.jsonl"))
    assert parsed.ok and parsed.text.startswith("Fixed `add()`")
    assert parsed.thread_id == "01a138ea-4477-72c3-949a-c3940e98b67c"
    assert (parsed.totals.input_tokens, parsed.totals.cached_input_tokens, parsed.totals.output_tokens) == (
        108725, 73216, 440,
    )
    assert "python3 -m unittest -q' -> exit 0" in parsed.log and "calc/ops.py" in parsed.log


def test_codex_failure_event_carries_the_api_error():
    parsed = parse_codex_events(fixture("codex_error.jsonl"))
    assert not parsed.ok and "not supported" in parsed.error and parsed.totals is None


def codex_workspace(stdout: str, model: str | None = None) -> ScriptedWorkspace:
    def handler(argv, stdin):
        if argv[0] == "codex":
            return ExecResult(0, stdout, "")
        if argv[:2] == ["sh", "-c"] and "turn_context" in argv[2] and model:
            return ExecResult(0, json.dumps({"payload": {"model": model}}) + "\n", "")
        return ExecResult(1, "", "")

    return ScriptedWorkspace(handler)


def test_codex_cost_from_explicit_or_discovered_model_else_none():
    expected = cost_usd("gpt-6-luna", input_tokens=108725, cached_input_tokens=73216, output_tokens=440)
    assert expected == pytest.approx((35509 * 0.10 + 73216 * 0.01 + 440 * 0.50) / 1e6)
    stdout = fixture("codex_success.jsonl")

    explicit = run(CodexHarness("gpt-6-luna").run(codex_workspace(stdout), "t"))
    assert explicit.usage.cost_usd == pytest.approx(expected) and explicit.model == "gpt-6-luna"

    discovered = run(CodexHarness().run(codex_workspace(stdout, "gpt-6-luna"), "t"))
    assert discovered.usage.cost_usd == pytest.approx(expected) and discovered.model == "gpt-6-luna"

    assert run(CodexHarness().run(codex_workspace(stdout), "t")).usage.cost_usd is None  # model unknown
    assert run(CodexHarness("unlisted-model").run(codex_workspace(stdout), "t")).usage.cost_usd is None


def test_codex_resume_reports_the_delta_of_the_cumulative_thread_usage():
    first, second = fixture("codex_success.jsonl"), fixture("codex_resume.jsonl")
    harness = CodexHarness("gpt-6-luna")
    a = run(harness.run(codex_workspace(first), "t"))
    b = run(harness.run(codex_workspace(second), "again", session_id=a.session_id))
    assert (b.usage.input_tokens, b.usage.cached_input_tokens, b.usage.output_tokens) == (
        127293 - 108725, 0, 501 - 440,
    )
    assert b.usage.cost_usd == pytest.approx(cost_usd("gpt-6-luna", input_tokens=18568, output_tokens=61))
    assert b.final_message.startswith('{"score"')

    # A thread this harness never saw has an unknown baseline: no guessed cost.
    stranger = run(CodexHarness("gpt-6-luna").run(codex_workspace(second), "again", session_id=a.session_id))
    assert stranger.usage.cost_usd is None


def test_codex_failed_turn_is_a_failed_run():
    ws = codex_workspace(fixture("codex_error.jsonl"))
    result = run(CodexHarness("no-such-model").run(ws, "t"))
    assert result.exit_code != 0 and "not supported" in result.final_message


# ---- Verdict extraction -------------------------------------------------------------------


def test_verdict_is_the_last_valid_object_in_prose_or_fences():
    text = 'Draft: {"score": 1, "passed": false, "feedback": "x"}\n```json\n{"score": 12, "passed": true, "feedback": "ok {braces}"}\n```'
    verdict = parse_verdict(text)
    assert verdict.score == 10 and verdict.passed and verdict.feedback == "ok {braces}"


@pytest.mark.parametrize("text", ["", "no json", '{"score": 5}', '{"score": "high", "passed": true, "feedback": "x"}'])
def test_invalid_verdict_text_raises(text):
    with pytest.raises(VerdictParseError):
        parse_verdict(text)

import json

import pytest
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage as SdkUsage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agent.teams import LocalWorktree, SdkHarness
from agent.teams.pricing import cost_usd

from .fakes import make_repo, run


def tool_call(name: str, call_id: str, **args) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(arguments=json.dumps(args), call_id=call_id, name=name, type="function_call")


def message(text: str) -> ResponseOutputMessage:
    part = ResponseOutputText(text=text, type="output_text", annotations=[])
    return ResponseOutputMessage(id="m", content=[part], role="assistant", status="completed", type="message")


class ScriptedModel(Model):
    """Offline stand-in for a model: replays one list of output items per request."""

    model = "gpt-6-luna"

    def __init__(self, steps):
        self.steps = list(steps)
        self.inputs = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id, conversation_id, prompt):
        self.inputs.append(list(input) if isinstance(input, list) else input)
        usage = SdkUsage(
            requests=1, input_tokens=1000, output_tokens=50, total_tokens=1050,
            input_tokens_details=InputTokensDetails(cached_tokens=200, cache_write_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        )
        return ModelResponse(output=self.steps.pop(0), usage=usage, response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def test_sdk_agent_edits_files_through_the_workspace_and_resumes_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    repo = make_repo(tmp_path / "repo")
    model = ScriptedModel([
        [tool_call("run_shell", "c1", command="echo one > out.txt && yes x | head -c 50000")],
        [tool_call("write_file", "c2", path="pkg/new.txt", content="hello\n")],
        [message("all done")],
        [message("second answer")],
    ])
    harness = SdkHarness(model)

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            first = await harness.run(ws, "make the files", role="coder")
            second = await harness.run(ws, "anything else?", session_id=first.session_id)
            return first, second, await ws.diff()

    first, second, patch = run(go())
    assert first.exit_code == 0 and first.final_message == "all done" and first.role == "coder"
    assert "out.txt" in patch and "pkg/new.txt" in patch and "+hello" in patch

    # Tool output reaches the model truncated, not as 50 kB of text.
    shell_output = next(i for i in model.inputs[1] if i.get("type") == "function_call_output")["output"]
    assert "[exit 0]" in shell_output and "chars omitted" in shell_output and len(shell_output) < 20_000

    # Usage is summed over the three requests; the model is priced from the table.
    assert (first.usage.input_tokens, first.usage.cached_input_tokens, first.usage.output_tokens) == (3000, 600, 150)
    assert first.usage.cost_usd == pytest.approx(
        3 * cost_usd("gpt-6-luna", input_tokens=1000, cached_input_tokens=200, output_tokens=50)
    )

    # Resuming replays the first conversation, then the new user turn.
    assert second.session_id == first.session_id and second.final_message == "second answer"
    replay = model.inputs[3]
    assert any("make the files" in str(i.get("content")) for i in replay if i.get("role") == "user")
    assert replay[-1]["content"] == "anything else?"


def test_sdk_run_failure_is_a_failed_run_not_an_exception(tmp_path, monkeypatch):
    class Broken(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            raise ConnectionError("no network")

    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    repo = make_repo(tmp_path / "repo")

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            return await SdkHarness(Broken([])).run(ws, "x")

    result = run(go())
    assert result.exit_code == 1 and "no network" in result.final_message


def test_litellm_models_need_the_litellm_extra(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_litellm(name, *args, **kwargs):
        if name.startswith("litellm") or name == "agents.extensions.models.litellm_model":
            raise ImportError("No module named 'litellm'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_litellm)
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    repo = make_repo(tmp_path / "repo")

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            return await SdkHarness("litellm/openrouter/some-model").run(ws, "x")

    with pytest.raises(RuntimeError, match="litellm extra"):
        run(go())


def test_usage_spent_before_an_sdk_error_is_still_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    repo = make_repo(tmp_path / "repo")
    loops_forever = ScriptedModel([[tool_call("run_shell", f"c{i}", command="true")] for i in range(5)])

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            return await SdkHarness(loops_forever, max_turns=3).run(ws, "x")

    result = run(go())
    assert result.exit_code == 1
    assert result.usage.input_tokens == 3000 and result.usage.output_tokens == 150
    assert result.usage.cost_usd is not None

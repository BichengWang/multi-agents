"""Built-in minimal coding agent on the OpenAI Agents SDK; runs any model via LiteLLM.

``sdk@<openai-model>`` uses the SDK's default OpenAI client; ``sdk@litellm/<provider>/<model>``
uses ``agents.extensions.models.litellm_model.LitellmModel`` (needs the ``litellm`` extra,
imported lazily). With no model, the SDK's default model is used.

The agent has two tools, both implemented with ``Workspace.exec`` so they work against remote
sandboxes as well: ``run_shell`` and ``write_file``. Unlike the CLI harnesses the SDK agent has
no permission system of its own: its shell commands run with the workspace's own privileges, so
only use it on workspaces that are themselves the sandbox. Model calls are made from the host
process (API keys come from the host environment), tool calls from the workspace; ``env`` is
merged into every tool command.

Resume: the conversation is kept in memory per session id for the lifetime of this harness
object. Tracing is disabled so no code is exported to a tracing backend.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..pricing import LONG_CONTEXT_THRESHOLD, cost_usd
from ..types import HarnessRun, Usage, Workspace
from .base import TIMEOUT_EXIT_CODE, clip

SYSTEM_PROMPT = (
    "You are a software engineer working in a git repository checked out at the current "
    "directory. Use run_shell to explore, run tests and edit with standard tools, and write_file "
    "to create or overwrite whole files. Make the smallest correct change that solves the task, "
    "verify it by running the relevant tests when they exist, and do not commit. When finished, "
    "reply with a brief summary of what you changed."
)

LITELLM_PREFIX = "litellm/"
MAX_TOOL_OUTPUT = 12_000
DEFAULT_SHELL_TIMEOUT = 300.0


def _load_litellm_model(name: str) -> Any:
    try:
        from agents.extensions.models.litellm_model import LitellmModel
    except ImportError as exc:
        raise RuntimeError(
            f"sdk@{LITELLM_PREFIX}{name} needs the litellm extra; install it with "
            "`uv sync --extra litellm` (or `pip install litellm`)"
        ) from exc
    return LitellmModel(model=name)


def _litellm_cost(model: str, input_tokens: int, cached: int, output_tokens: int) -> Optional[float]:
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            cache_read_input_tokens=cached,
        )
        return float(prompt_cost) + float(completion_cost)
    except Exception:
        return None


@dataclass
class SdkHarness:
    """``sdk@<model>``. ``model`` may also be an ``agents`` ``Model`` instance (used in tests)."""

    model: Any = None
    env: dict[str, str] = field(default_factory=dict)
    max_turns: int = 60
    shell_timeout: float = DEFAULT_SHELL_TIMEOUT
    name: str = field(default="sdk", init=False)
    _sessions: dict[str, list[Any]] = field(default_factory=dict, init=False, repr=False)

    def _model_name(self) -> Optional[str]:
        if isinstance(self.model, str):
            return self.model
        return getattr(self.model, "model", None) if self.model is not None else None

    def _resolve_model(self) -> Any:
        if isinstance(self.model, str) and self.model.startswith(LITELLM_PREFIX):
            return _load_litellm_model(self.model[len(LITELLM_PREFIX):])
        if self.model is None:
            from agents.models import get_default_model

            return get_default_model()
        return self.model

    def _tools(self, workspace: Workspace) -> list[Any]:
        from agents import function_tool

        env = self.env or None
        default_timeout = self.shell_timeout

        @function_tool
        async def run_shell(command: str, timeout_s: Optional[float] = None) -> str:
            """Run a shell command (sh -c) in the repository root and return its exit code and output.

            Args:
                command: The shell command line to run.
                timeout_s: Optional time limit in seconds (default and maximum 300).
            """
            limit = min(timeout_s or default_timeout, default_timeout)
            try:
                res = await workspace.exec(["sh", "-c", command], env=env, timeout=limit)
            except (TimeoutError, OSError) as exc:
                return f"[error] {type(exc).__name__}: {exc}"
            return clip(f"[exit {res.exit_code}]\n{res.stdout}{res.stderr}", MAX_TOOL_OUTPUT)

        @function_tool
        async def write_file(path: str, content: str) -> str:
            """Create or overwrite a file (parent directories are created).

            Args:
                path: File path, relative to the repository root.
                content: The complete new file content.
            """
            res = await workspace.exec(
                ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
                env=env,
                stdin=content,
                timeout=60,
            )
            if res.exit_code != 0:
                return f"[error] exit {res.exit_code}: {res.stderr.strip()}"
            return f"wrote {len(content)} characters to {path}"

        return [run_shell, write_file]

    async def run(
        self,
        workspace: Workspace,
        prompt: str,
        *,
        role: str = "worker",
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HarnessRun:
        from agents import Agent, RunConfig, Runner

        start = time.perf_counter()
        history = list(self._sessions.get(session_id, [])) if session_id else []
        sid = session_id if session_id in self._sessions else uuid.uuid4().hex
        name = self._model_name()
        result: Any = None
        try:
            agent = Agent(
                name=f"sdk-{role}",
                instructions=SYSTEM_PROMPT,
                tools=self._tools(workspace),
                model=self._resolve_model(),
            )
            result = await asyncio.wait_for(
                Runner.run(
                    agent,
                    [*history, {"role": "user", "content": prompt}],
                    max_turns=self.max_turns,
                    run_config=RunConfig(tracing_disabled=True),
                ),
                timeout,
            )
        except asyncio.TimeoutError:
            return self._failed(role, name, f"timed out after {timeout:g}s", TIMEOUT_EXIT_CODE, start, sid)
        except RuntimeError as exc:
            if "litellm extra" in str(exc):
                raise
            return self._failed(role, name, f"{type(exc).__name__}: {exc}", 1, start, sid)
        except Exception as exc:
            return self._failed(role, name, f"{type(exc).__name__}: {exc}", 1, start, sid)

        self._sessions[sid] = result.to_input_list()
        usage = self._usage(result.context_wrapper.usage, name)
        return HarnessRun(
            harness="sdk",
            model=name,
            role=role,
            final_message=str(result.final_output or ""),
            exit_code=0,
            usage=usage,
            duration_s=time.perf_counter() - start,
            session_id=sid,
        )

    def _failed(
        self, role: str, name: Optional[str], message: str, code: int, start: float, sid: str
    ) -> HarnessRun:
        return HarnessRun(
            "sdk", name, role, message, code, Usage(), time.perf_counter() - start, sid, message
        )

    def _usage(self, sdk_usage: Any, name: Optional[str]) -> Usage:
        is_litellm = isinstance(self.model, str) and self.model.startswith(LITELLM_PREFIX)
        model_id = name[len(LITELLM_PREFIX):] if is_litellm and name else name
        entries = sdk_usage.request_usage_entries or []
        cost: Optional[float] = 0.0
        for e in entries:
            cached = e.input_tokens_details.cached_tokens or 0
            written = getattr(e.input_tokens_details, "cache_write_tokens", 0) or 0
            if is_litellm:
                part = _litellm_cost(model_id or "", e.input_tokens, cached, e.output_tokens)
            else:
                part = cost_usd(
                    model_id,
                    input_tokens=e.input_tokens,
                    cached_input_tokens=cached,
                    cache_write_tokens=written,
                    output_tokens=e.output_tokens,
                    long_context=e.input_tokens > LONG_CONTEXT_THRESHOLD,
                )
            cost = None if part is None or cost is None else cost + part
        if not entries and sdk_usage.input_tokens:
            cost = None
        return Usage(
            sdk_usage.input_tokens,
            sdk_usage.input_tokens_details.cached_tokens or 0,
            sdk_usage.output_tokens,
            cost,
        )

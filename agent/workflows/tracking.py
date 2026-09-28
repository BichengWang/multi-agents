from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .base import AgentRunner, agent_name


@dataclass
class CallRecord:
    """Timing (and, with the SDK runner, token usage) for one agent call."""

    agent: str
    started_at: str
    duration_s: float
    usage: Optional[dict[str, int]] = None
    error: Optional[str] = None


def _usage_dict(usage: Any) -> dict[str, int]:
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


@dataclass
class RunTracker:
    """A runner that records every agent call it makes.

    With no ``base`` it runs agents through the OpenAI Agents SDK itself, wraps each call in a
    tracing span, and captures token usage. With a ``base`` runner (e.g. a test fake) it only
    records timings. Pass the tracker anywhere an ``AgentRunner`` is accepted.
    """

    base: Optional[AgentRunner] = None
    calls: list[CallRecord] = field(default_factory=list)

    async def __call__(self, agent: Any, input_text: str) -> Any:
        record = CallRecord(agent_name(agent), datetime.now(timezone.utc).isoformat(), 0.0)
        start = time.perf_counter()
        try:
            if self.base is not None:
                return await self.base(agent, input_text)
            from agents import Runner, custom_span

            with custom_span(f"agent:{record.agent}"):
                result = await Runner.run(agent, input_text)
            record.usage = _usage_dict(result.context_wrapper.usage)
            return result.final_output
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_s = round(time.perf_counter() - start, 3)
            self.calls.append(record)

    def totals(self) -> dict[str, Any]:
        tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for call in self.calls:
            for key in tokens:
                tokens[key] += (call.usage or {}).get(key, 0)
        return {
            "calls": len(self.calls),
            "errors": sum(1 for c in self.calls if c.error),
            "agent_seconds": round(sum(c.duration_s for c in self.calls), 3),
            **tokens,
        }

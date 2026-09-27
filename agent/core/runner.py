from __future__ import annotations

import asyncio
import logging
from typing import Any

from agents import Agent, Runner, RunResult

logger = logging.getLogger(__name__)


async def run_agent(
    agent: Agent[Any],
    input: str,
    *,
    max_retries: int = 2,
    timeout: float | None = 300,
    **kwargs: Any,
) -> RunResult:
    """Run an agent with a per-attempt timeout and exponential-backoff retries."""
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(Runner.run(agent, input, **kwargs), timeout=timeout)
        except Exception as exc:
            if attempt == max_retries:
                raise
            delay = 2**attempt
            logger.warning(
                "%s failed (attempt %d/%d): %s; retrying in %ds",
                agent.name, attempt + 1, max_retries + 1, exc, delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")

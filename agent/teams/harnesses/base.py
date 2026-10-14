"""Shared plumbing for harnesses that drive an external CLI through ``Workspace.exec``."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal, Optional

from ..types import ExecResult, Workspace

Permissions = Literal["safe", "bypass"]
PERMISSIONS: tuple[str, ...] = ("safe", "bypass")

TIMEOUT_EXIT_CODE = 124
MISSING_BINARY_EXIT_CODE = 127
LOG_LIMIT = 20_000


@dataclass
class CliOutcome:
    """What a CLI invocation did: its result, or why there is none."""

    result: Optional[ExecResult]
    duration_s: float
    error: Optional[str] = None
    exit_code: int = 0
    partial_stdout: str = ""
    partial_stderr: str = ""


def check_permissions(value: str) -> Permissions:
    if value not in PERMISSIONS:
        raise ValueError(f"permissions must be one of {', '.join(PERMISSIONS)}; got {value!r}")
    return value  # type: ignore[return-value]


def clip(text: str, limit: int = LOG_LIMIT) -> str:
    """Keep the head and tail of long text so both the start and the failure are visible."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars omitted] ...\n{text[-half:]}"


async def run_cli(
    workspace: Workspace,
    argv: list[str],
    *,
    stdin: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> CliOutcome:
    """Run ``argv`` in ``workspace``; timeouts and a missing binary become outcomes, not raises."""
    start = time.perf_counter()
    try:
        result = await workspace.exec(argv, env=env, timeout=timeout, stdin=stdin)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        return CliOutcome(
            None,
            time.perf_counter() - start,
            f"timed out after {timeout:g}s" if timeout else "timed out",
            TIMEOUT_EXIT_CODE,
            getattr(exc, "stdout", "") or "",
            getattr(exc, "stderr", "") or "",
        )
    except FileNotFoundError:
        return CliOutcome(
            None, time.perf_counter() - start, f"{argv[0]!r} not found on PATH", MISSING_BINARY_EXIT_CODE
        )
    return CliOutcome(result, time.perf_counter() - start, exit_code=result.exit_code)

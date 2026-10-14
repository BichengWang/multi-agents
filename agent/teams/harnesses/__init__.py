"""Harness adapters: external coding CLIs, the built-in SDK agent and the shell-command check."""

from .check import Check, CheckHarness, CheckResult, CheckRunner
from .claude import ClaudeHarness
from .codex import CodexHarness
from .sdk import SdkHarness

__all__ = [
    "Check",
    "CheckHarness",
    "CheckResult",
    "CheckRunner",
    "ClaudeHarness",
    "CodexHarness",
    "SdkHarness",
]

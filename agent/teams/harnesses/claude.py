"""Claude Code CLI harness (``claude -p --output-format json``).

Verified against claude 2.1.296. One invocation = one ``claude -p`` process; the prompt goes on
stdin, the result is a single JSON object on stdout.

Permissions
-----------
``safe`` (default): ``--permission-mode acceptEdits`` plus Claude Code's own OS sandbox
(``sandbox.enabled`` with ``autoAllowBashIfSandboxed``). File edits are auto-approved, Bash
commands (tests, builds) run automatically *inside the sandbox*, which confines writes to the
workspace and blocks network. Anything that would still need a prompt is denied
(``--permission-prompts none``) instead of hanging. If the sandbox is unavailable on the host,
Bash falls back to requiring approval, i.e. it is denied: the agent can edit but not run tests.

``bypass``: ``--dangerously-skip-permissions``. Only for externally sandboxed workspaces. The CLI
refuses it as root unless ``IS_SANDBOX=1`` is in its environment; pass that via ``env``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..types import HarnessRun, Usage, Workspace
from .base import Permissions, check_permissions, clip, run_cli

SANDBOX_SETTINGS = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: Optional[str]
    usage: Usage
    model: Optional[str]
    detail: str = ""


def parse_claude_output(stdout: str) -> ClaudeResult:
    """Parse the ``--output-format json`` result object.

    On ``--resume`` the session id is unchanged and ``modelUsage`` and ``total_cost_usd`` are
    cumulative over the whole session (checked live against claude 2.1.296: output tokens and cost
    of a resumed run include the earlier run; see ``claude_resume_*.json`` fixtures), while the
    top-level ``usage`` is per process. ``ClaudeHarness`` therefore subtracts the session total
    seen at the end of the previous run.

    Tokens come from ``modelUsage`` when present (the top-level ``usage`` is zeroed on some error
    results) and count cache reads and cache creation as input; ``cached_input_tokens`` is the
    cache-read part. Cost is the CLI's own ``total_cost_usd``; missing means unpriced (None).
    """
    data = _load_object(stdout)
    if data is None:
        raise ValueError("claude did not print a JSON result object")
    model_usage: dict[str, Any] = data.get("modelUsage") or {}
    if model_usage:
        inp = sum(
            u.get("inputTokens", 0) + u.get("cacheReadInputTokens", 0) + u.get("cacheCreationInputTokens", 0)
            for u in model_usage.values()
        )
        cached = sum(u.get("cacheReadInputTokens", 0) for u in model_usage.values())
        out = sum(u.get("outputTokens", 0) for u in model_usage.values())
        model = max(model_usage, key=lambda m: model_usage[m].get("costUSD", 0.0))
    else:
        u = data.get("usage") or {}
        cached = u.get("cache_read_input_tokens", 0)
        inp = u.get("input_tokens", 0) + cached + u.get("cache_creation_input_tokens", 0)
        out = u.get("output_tokens", 0)
        model = None
    cost = data.get("total_cost_usd")
    usage = Usage(inp, cached, out, float(cost) if isinstance(cost, (int, float)) else None)

    result = data.get("result")
    if not isinstance(result, str) or not result:
        structured = data.get("structured_output")
        result = json.dumps(structured) if structured is not None else ""
    errors = data.get("errors") or []
    ok = not data.get("is_error") and data.get("subtype", "success") == "success"
    if not ok and not result:
        result = "; ".join(str(e) for e in errors) or str(data.get("subtype") or "error")
    denials = data.get("permission_denials") or []
    detail = f"turns={data.get('num_turns')} stop={data.get('terminal_reason') or data.get('stop_reason')}"
    if denials:
        detail += " denied=" + ",".join(
            f"{d.get('tool_name', '?')}" for d in denials if isinstance(d, dict)
        )
    return ClaudeResult(ok, result, data.get("session_id"), usage, model, detail)


def _load_object(stdout: str) -> Optional[dict[str, Any]]:
    text = stdout.strip()
    for candidate in (text, *reversed(text.splitlines())):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class ClaudeHarness:
    """``claude@<model>`` (alias such as ``haiku``/``sonnet`` or a full model name)."""

    model: Optional[str] = None
    permissions: Permissions = "safe"
    env: dict[str, str] = field(default_factory=dict)
    max_budget_usd: Optional[float] = None
    name: str = field(default="claude", init=False)
    supports_output_schema: bool = field(default=True, init=False)
    # session id -> cumulative usage reported at the end of its latest run
    _totals: dict[str, Usage] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.permissions = check_permissions(self.permissions)

    def argv(self, session_id: Optional[str], output_schema: Optional[dict[str, Any]]) -> list[str]:
        argv = ["claude", "-p", "--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        if self.permissions == "bypass":
            argv.append("--dangerously-skip-permissions")
        else:
            argv += [
                "--permission-mode",
                "acceptEdits",
                "--permission-prompts",
                "none",
                "--settings",
                SANDBOX_SETTINGS,
            ]
        if session_id:
            argv += ["--resume", session_id]
        if output_schema is not None:
            argv += ["--json-schema", json.dumps(output_schema)]
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        return argv

    def _session_delta(self, new_id: Optional[str], resumed_from: Optional[str], total: Usage) -> Usage:
        """This run's share of the cumulative session usage; unknown baseline means unpriced."""
        base = self._totals.get(resumed_from) if resumed_from else None
        if new_id:
            self._totals[new_id] = total
        if not resumed_from:
            return total
        if base is None:
            return Usage(total.input_tokens, total.cached_input_tokens, total.output_tokens, None)
        delta = Usage(
            total.input_tokens - base.input_tokens,
            total.cached_input_tokens - base.cached_input_tokens,
            total.output_tokens - base.output_tokens,
            None if total.cost_usd is None or base.cost_usd is None else total.cost_usd - base.cost_usd,
        )
        if min(delta.input_tokens, delta.cached_input_tokens, delta.output_tokens, delta.cost_usd or 0) < 0:
            return Usage(total.input_tokens, total.cached_input_tokens, total.output_tokens, None)
        return delta

    async def run(
        self,
        workspace: Workspace,
        prompt: str,
        *,
        role: str = "worker",
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
        output_schema: Optional[dict[str, Any]] = None,
    ) -> HarnessRun:
        outcome = await run_cli(
            workspace, self.argv(session_id, output_schema), stdin=prompt, env=self.env, timeout=timeout
        )

        def failed(message: str, exit_code: int, log: str = "") -> HarnessRun:
            return HarnessRun(
                "claude", self.model, role, message, exit_code or 1, Usage(), outcome.duration_s,
                session_id, clip(log),
            )

        if outcome.result is None:
            return failed(outcome.error or "claude did not run", outcome.exit_code, outcome.partial_stdout)
        res = outcome.result
        try:
            parsed = parse_claude_output(res.stdout)
        except ValueError as exc:
            log = f"{res.stderr}\n{res.stdout}".strip()
            return failed(f"{exc}: {clip(log, 500)}", res.exit_code, log)
        exit_code = res.exit_code if res.exit_code != 0 else (0 if parsed.ok else 1)
        usage = self._session_delta(parsed.session_id, session_id, parsed.usage)
        log = f"{parsed.detail}\n{res.stderr}".strip()
        return HarnessRun(
            harness="claude",
            model=parsed.model or self.model,
            role=role,
            final_message=parsed.text,
            exit_code=exit_code,
            usage=usage,
            duration_s=outcome.duration_s,
            session_id=parsed.session_id or session_id,
            log=clip(log),
        )

"""Codex CLI harness (``codex exec --json``).

Verified against codex-cli 0.162.1. The prompt goes on stdin (``-``); stdout is a JSONL event
stream: ``thread.started`` (thread id), ``item.completed`` items (``agent_message``,
``command_execution``, ``file_change``, ``error`` warnings), ``turn.completed`` (usage) or
``turn.failed``.

Permissions
-----------
``safe`` (default): ``-s workspace-write`` (writes confined to the workspace, no network) with
``approval_policy="never"`` so nothing can block on a prompt. ``bypass``:
``--dangerously-bypass-approvals-and-sandbox``, only for externally sandboxed workspaces.

Usage and cost
--------------
``turn.completed.usage`` is the *cumulative* total of the whole thread, so on resume the harness
subtracts what it saw at the end of the previous run of that thread; resuming a thread this
instance has not seen before gives an unknown baseline and therefore no cost (None). Input tokens
include cached ones. The stream does not name the model: when none was requested the effective
model is read from the session's rollout file (``turn_context.model``) through ``exec``; if that
is impossible, or the model is not in ``pricing.py``, cost is None.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..pricing import cost_usd
from ..types import HarnessRun, Usage, Workspace
from .base import Permissions, check_permissions, clip, run_cli

_FIND_ROLLOUT = (
    'f=$(find "${CODEX_HOME:-$HOME/.codex}/sessions" -name "*$1.jsonl" 2>/dev/null | head -n 1); '
    '[ -n "$f" ] && grep -m 1 \'"type":"turn_context"\' "$f"'
)


@dataclass
class CodexTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def minus(self, other: CodexTotals) -> CodexTotals:
        return CodexTotals(
            self.input_tokens - other.input_tokens,
            self.cached_input_tokens - other.cached_input_tokens,
            self.cache_write_tokens - other.cache_write_tokens,
            self.output_tokens - other.output_tokens,
        )


@dataclass
class CodexResult:
    ok: bool
    text: str
    thread_id: Optional[str]
    totals: Optional[CodexTotals]
    error: str = ""
    log: str = ""


def parse_codex_events(stdout: str) -> CodexResult:
    """Fold the JSONL stream into the final message, thread id, cumulative usage and a short log."""
    thread_id: Optional[str] = None
    text = ""
    totals: Optional[CodexTotals] = None
    error = ""
    failed = False
    completed = False
    log: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "turn.completed":
            completed = True
            u = event.get("usage") or {}
            totals = CodexTotals(
                u.get("input_tokens", 0),
                u.get("cached_input_tokens", 0),
                u.get("cache_write_input_tokens", 0),
                u.get("output_tokens", 0),
            )
        elif kind in ("turn.failed", "error"):
            failed = failed or kind == "turn.failed"
            message = event.get("message") or (event.get("error") or {}).get("message") or ""
            error = _error_text(message) or error
            log.append(f"[{kind}] {error}")
        elif kind == "item.completed":
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                text = item.get("text", text)
            elif itype == "command_execution":
                log.append(f"$ {item.get('command')} -> exit {item.get('exit_code')}")
            elif itype == "file_change":
                paths = ", ".join(c.get("path", "?") for c in item.get("changes") or [])
                log.append(f"[file_change] {paths}")
            elif itype == "error":
                log.append(f"[warning] {item.get('message')}")
    ok = completed and not failed
    if not ok and not error:
        error = "codex exited without completing the turn"
    return CodexResult(ok, text, thread_id, totals, error, clip("\n".join(log)))


def _error_text(message: Any) -> str:
    """Codex wraps API errors as a JSON string; surface the inner message when it parses."""
    if not isinstance(message, str):
        return str(message)
    try:
        inner = json.loads(message)
    except ValueError:
        return message
    if isinstance(inner, dict):
        err = inner.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if inner.get("message"):
            return str(inner["message"])
    return message


@dataclass
class CodexHarness:
    """``codex@<model>``; with no model the CLI's configured default is used."""

    model: Optional[str] = None
    permissions: Permissions = "safe"
    env: dict[str, str] = field(default_factory=dict)
    name: str = field(default="codex", init=False)
    supports_output_schema: bool = field(default=True, init=False)
    _totals: dict[str, CodexTotals] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.permissions = check_permissions(self.permissions)

    def argv(self, session_id: Optional[str], schema_path: Optional[str]) -> list[str]:
        common = ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"']
        if self.model:
            common += ["-m", self.model]
        if schema_path:
            common += ["--output-schema", schema_path]
        if session_id:
            argv = ["codex", "exec", "resume", session_id, "-", *common]
            # `exec resume` has no -s flag; the same policy goes through -c.
            if self.permissions == "bypass":
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                argv += ["-c", 'sandbox_mode="workspace-write"']
            return argv
        argv = ["codex", "exec", "-", *common]
        if self.permissions == "bypass":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["-s", "workspace-write"]
        return argv

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
        schema_path: Optional[str] = None
        if output_schema is not None:
            # --output-schema takes a file; write it on the workspace's side, outside the repo.
            schema_path = f"/tmp/multi-agents-{uuid.uuid4().hex}.schema.json"
            wrote = await workspace.exec(
                ["sh", "-c", 'cat > "$1"', "sh", schema_path], stdin=json.dumps(output_schema)
            )
            if wrote.exit_code != 0:
                raise RuntimeError(f"could not write the output schema: {wrote.stderr.strip()}")
        try:
            outcome = await run_cli(
                workspace, self.argv(session_id, schema_path), stdin=prompt, env=self.env, timeout=timeout
            )
        finally:
            if schema_path:
                await workspace.exec(["rm", "-f", schema_path])

        def failed(message: str, exit_code: int, log: str = "") -> HarnessRun:
            return HarnessRun(
                "codex", self.model, role, message, exit_code or 1, Usage(), outcome.duration_s,
                session_id, clip(log),
            )

        if outcome.result is None:
            return failed(outcome.error or "codex did not run", outcome.exit_code, outcome.partial_stdout)
        res = outcome.result
        parsed = parse_codex_events(res.stdout)
        thread_id = parsed.thread_id or session_id
        usage, model = await self._usage(workspace, parsed, thread_id, session_id)
        exit_code = res.exit_code if res.exit_code != 0 else (0 if parsed.ok else 1)
        message = parsed.text if parsed.ok or parsed.text else parsed.error
        log = parsed.log
        if res.exit_code != 0 and res.stderr.strip():
            log = f"{log}\n{res.stderr.strip()}"
        return HarnessRun(
            harness="codex",
            model=model,
            role=role,
            final_message=message,
            exit_code=exit_code,
            usage=usage,
            duration_s=outcome.duration_s,
            session_id=thread_id,
            log=clip(log),
        )

    async def _usage(
        self,
        workspace: Workspace,
        parsed: CodexResult,
        thread_id: Optional[str],
        resumed_from: Optional[str],
    ) -> tuple[Usage, Optional[str]]:
        model = self.model
        if parsed.totals is None:
            return Usage(), model
        totals = parsed.totals
        delta: Optional[CodexTotals] = totals
        if resumed_from:
            baseline = self._totals.get(resumed_from)
            delta = totals.minus(baseline) if baseline else None
        if thread_id:
            self._totals[thread_id] = totals
        if model is None and thread_id:
            model = await self._effective_model(workspace, thread_id)
        cost = None
        if delta is not None:
            cost = cost_usd(
                model,
                input_tokens=delta.input_tokens,
                cached_input_tokens=delta.cached_input_tokens,
                cache_write_tokens=delta.cache_write_tokens,
                output_tokens=delta.output_tokens,
            )
        # Unknown baseline: report the thread's cumulative tokens, unpriced.
        shown = delta or totals
        return Usage(shown.input_tokens, shown.cached_input_tokens, shown.output_tokens, cost), model

    async def _effective_model(self, workspace: Workspace, thread_id: str) -> Optional[str]:
        try:
            res = await workspace.exec(["sh", "-c", _FIND_ROLLOUT, "sh", thread_id], env=self.env, timeout=30)
        except Exception:
            return None
        if res.exit_code != 0 or not res.stdout.strip():
            return None
        try:
            model = json.loads(res.stdout.splitlines()[0])["payload"]["model"]
        except (ValueError, KeyError, TypeError):
            return None
        return model if isinstance(model, str) and model else None

"""Structured reviewer/judge verdicts from any harness.

Reviewers and judges are asked for a JSON object matching ``agent.workflows.Verdict``. Harnesses
with a native output-schema flag (``supports_output_schema``) get the schema through it; for every
harness the answer is also parsed by scanning the final message for the last valid JSON object,
so a prose-wrapped or code-fenced answer still counts. Anything unparseable is a *failed* review:
it is recorded, scores 0 and never crashes the team.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from agent.workflows import Verdict

from .types import Harness, HarnessRun, Workspace

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "number",
            "description": "Overall quality from 0 (broken or empty) to 10 (correct and complete).",
        },
        "passed": {
            "type": "boolean",
            "description": "True only if the change fully solves the task and needs no revision.",
        },
        "feedback": {
            "type": "string",
            "description": "Concrete, actionable feedback; what is wrong and how to fix it.",
        },
    },
    "required": ["score", "passed", "feedback"],
    "additionalProperties": False,
}

VERDICT_INSTRUCTIONS = (
    "Finish with a verdict as a single JSON object and nothing after it:\n"
    '{"score": <number 0-10>, "passed": <true|false>, "feedback": "<concrete, actionable feedback>"}\n'
    "Score 0 means broken or no change, 10 means correct and complete. Set passed to true only if "
    "the change fully solves the task and needs no revision."
)


class VerdictParseError(ValueError):
    pass


def parse_verdict(text: str) -> Verdict:
    """Last JSON object in ``text`` with ``score``/``passed``/``feedback``; score clamped to 0-10."""
    decoder = json.JSONDecoder()
    found: Optional[Verdict] = None
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        idx = end
        if isinstance(obj, dict) and {"score", "passed", "feedback"} <= obj.keys():
            try:
                verdict = Verdict.model_validate(obj)
            except ValueError:
                continue
            found = verdict.model_copy(update={"score": min(max(verdict.score, 0.0), 10.0)})
    if found is None:
        raise VerdictParseError("no JSON object with score/passed/feedback in the reply")
    return found


@dataclass
class VerdictOutcome:
    verdict: Verdict
    run: Optional[HarnessRun]
    # Set when the harness failed or its reply was not a valid verdict (the verdict is then a
    # zero-score placeholder carrying the error as feedback).
    error: Optional[str] = None


async def request_verdict(
    harness: Harness,
    workspace: Workspace,
    prompt: str,
    *,
    role: str,
    timeout: Optional[float] = None,
) -> VerdictOutcome:
    """Run ``harness`` as ``role`` and parse its verdict; failures become zero-score outcomes."""
    kwargs: dict[str, Any] = {}
    if getattr(harness, "supports_output_schema", False):
        kwargs["output_schema"] = VERDICT_SCHEMA
    run: Optional[HarnessRun] = None
    try:
        run = await harness.run(
            workspace, f"{prompt}\n\n{VERDICT_INSTRUCTIONS}", role=role, timeout=timeout, **kwargs
        )
        if run.exit_code != 0:
            raise RuntimeError(f"exit code {run.exit_code}: {run.final_message[:300]}")
        return VerdictOutcome(parse_verdict(run.final_message), run)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return VerdictOutcome(
            Verdict(score=0.0, passed=False, feedback=f"The {role} failed to produce a verdict ({error})."),
            run,
            error,
        )

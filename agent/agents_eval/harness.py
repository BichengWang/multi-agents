"""Async evaluation harness that scores real agent workflows over a JSONL dataset.

A workflow is any ``async (input: str) -> str`` callable. Each case output is scored by a
list of checks; each check returns a 0-1 score and a pass/fail. Reports are saved as one
JSON file per run and can be compared to see whether a prompt or model change helped.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agents import Agent

from agent.core import Evaluation, runner

Workflow = Callable[[str], Awaitable[str]]


@dataclass
class EvalCase:
    id: str
    input: str
    rubric: str = ""
    """Optional case-specific grading guidance passed to LLM judges."""
    expected_keywords: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    score: float
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    output: str
    checks: list[CheckResult]
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(check.passed for check in self.checks)


class Check(Protocol):
    name: str

    async def __call__(self, case: EvalCase, output: str) -> CheckResult: ...


def load_cases(path: str | Path) -> list[EvalCase]:
    cases = []
    with open(path) as f:
        for line in f:
            if line.strip():
                cases.append(EvalCase(**json.loads(line)))
    return cases


# --- Deterministic checks -------------------------------------------------------------


@dataclass
class LengthCheck:
    min_chars: int = 1
    max_chars: int | None = None
    name: str = "length"

    async def __call__(self, case: EvalCase, output: str) -> CheckResult:
        n = len(output.strip())
        ok = n >= self.min_chars and (self.max_chars is None or n <= self.max_chars)
        return CheckResult(self.name, 1.0 if ok else 0.0, ok, f"{n} chars")


@dataclass
class KeywordCheck:
    """Fraction of the case's ``expected_keywords`` present in the output (case-insensitive)."""

    min_ratio: float = 1.0
    name: str = "keywords"

    async def __call__(self, case: EvalCase, output: str) -> CheckResult:
        if not case.expected_keywords:
            return CheckResult(self.name, 1.0, True, "no keywords expected")
        lowered = output.lower()
        missing = [k for k in case.expected_keywords if k.lower() not in lowered]
        ratio = 1 - len(missing) / len(case.expected_keywords)
        return CheckResult(self.name, ratio, ratio >= self.min_ratio, f"missing: {missing}" if missing else "")


# --- LLM judge ------------------------------------------------------------------------


JUDGE_PROMPT = (
    "You are a strict, impartial grader. You receive a task, optional grading rubric, and a "
    "candidate response. Grade only the response against the task and rubric. Return a "
    "structured verdict: score 1-10, passed=true only if the response fully satisfies the "
    "task, strengths, concrete issues, and suggestions."
)


def make_judge_agent(model: str | None = None) -> Agent[Any]:
    from agent.config import model_for

    return Agent(
        name="EvalJudgeAgent",
        instructions=JUDGE_PROMPT,
        model=model or model_for("judge"),
        output_type=Evaluation,
    )


@dataclass
class LLMJudgeCheck:
    judge: Agent[Any] = field(default_factory=make_judge_agent)
    threshold: int = 7
    name: str = "llm_judge"

    async def __call__(self, case: EvalCase, output: str) -> CheckResult:
        prompt = f"Task:\n{case.input}\n\n"
        if case.rubric:
            prompt += f"Rubric:\n{case.rubric}\n\n"
        prompt += f"Response:\n{output}"
        verdict = (await runner.run_agent(self.judge, prompt)).final_output_as(Evaluation)
        ok = verdict.passed and verdict.score >= self.threshold
        return CheckResult(self.name, verdict.score / 10, ok, "; ".join(verdict.issues))


# --- Running and reporting ------------------------------------------------------------


@dataclass
class EvalReport:
    name: str
    created_at: str
    results: list[CaseResult]

    def summary(self) -> dict[str, Any]:
        per_check: dict[str, list[float]] = {}
        for result in self.results:
            for check in result.checks:
                per_check.setdefault(check.name, []).append(check.score)
        total = len(self.results)
        return {
            "cases": total,
            "errors": sum(1 for r in self.results if r.error),
            "pass_rate": sum(1 for r in self.results if r.passed) / total if total else 0.0,
            "mean_scores": {name: sum(s) / len(s) for name, s in per_check.items()},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "summary": self.summary(),
            "results": [asdict(r) | {"passed": r.passed} for r in self.results],
        }

    def save(self, output_dir: str | Path) -> Path:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = self.created_at.replace(":", "").replace("-", "")[:15]
        path = out / f"{self.name}_{stamp}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


async def run_eval(
    name: str,
    workflow: Workflow,
    cases: Sequence[EvalCase],
    checks: Sequence[Check],
    *,
    concurrency: int = 4,
) -> EvalReport:
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(case: EvalCase) -> CaseResult:
        async with semaphore:
            try:
                output = await workflow(case.input)
            except Exception as exc:
                return CaseResult(case.id, "", [], error=f"{type(exc).__name__}: {exc}")
            check_results = []
            for check in checks:
                try:
                    check_results.append(await check(case, output))
                except Exception as exc:
                    check_results.append(CheckResult(check.name, 0.0, False, f"check error: {exc}"))
            return CaseResult(case.id, output, check_results)

    results = await asyncio.gather(*(_one(case) for case in cases))
    return EvalReport(name, datetime.now(timezone.utc).isoformat(), list(results))


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Diff two saved report dicts: pass rate and per-check mean score deltas."""
    base, cand = baseline["summary"], candidate["summary"]
    checks = sorted(set(base["mean_scores"]) | set(cand["mean_scores"]))
    return {
        "pass_rate_delta": cand["pass_rate"] - base["pass_rate"],
        "mean_score_deltas": {
            name: cand["mean_scores"].get(name, 0.0) - base["mean_scores"].get(name, 0.0)
            for name in checks
        },
    }

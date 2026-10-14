"""Benchmark orchestration: task x team pairs -> sandbox -> team -> patch -> hidden tests -> JSONL.

``results.jsonl`` in the output directory holds one record per (task, team) pair. The run is
resumable: pairs with a finished record are skipped; pairs that errored are retried (an error in the
evaluation stage re-grades the saved patch instead of paying for the agents again).

Budget guard (``max_cost_usd``): before each pair starts, total priced spend so far (including
previous runs in the same output directory) is compared with the budget and no new pair starts once
it is reached. Pairs already running finish, so the final spend can exceed the budget by up to
``concurrency`` pairs. A budgeted run refuses to start when a team contains a harness whose cost
cannot be priced unless ``allow_unpriced`` is set, and stops launching as soon as a finished pair
reports an unpriced cost, because the budget can no longer be enforced.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.teams.types import Team, TeamResult, Workspace
from agent.workflows.concurrency import map_concurrently

from .dataset import SOURCES, BenchTask
from .evaluate import EvalResult, evaluate_patch
from .sandbox import ModalBackend, ModalSandboxWorkspace, SandboxConfig, SandboxExecTimeout

RESULTS_FILE = "results.jsonl"
META_FILE = "meta.json"
ARTIFACTS_DIR = "artifacts"
FINAL_MESSAGE_CHARS = 1000

TeamFactory = Callable[[str], Team]
WorkspaceFactory = Callable[[BenchTask], Awaitable[Workspace]]
Evaluator = Callable[[BenchTask, str], Awaitable[EvalResult]]
UnpricedFn = Callable[[str], list[str]]


class BudgetError(RuntimeError):
    """The run cannot honour ``--max-cost-usd`` (unpriced harness in a budgeted run)."""


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+=@,-]+", "_", text).strip("_") or "x"


def default_team_factory(spec: str) -> Team:
    from agent.teams import build_team  # lazy: the team runner is a separate module

    # The sandbox is the isolation boundary, so CLIs run with permissions bypassed.
    return build_team(spec, permissions="bypass")


def harness_specs(spec: str) -> list[str]:
    """Every harness (``harness[@model]``) a team spec uses, via the runner's own parser."""
    from agent.teams import parse_team_spec

    return [str(h) for h in parse_team_spec(spec).harness_specs]


def default_unpriced(spec: str) -> list[str]:
    from agent.teams import is_priced

    return [h for h in harness_specs(spec) if not is_priced(h)]


def required_secret_keys(specs: Sequence[str], harnesses: Callable[[str], list[str]] = harness_specs) -> tuple[str, ...]:
    """API keys the Modal Secret must hold for these teams (checked by Modal at sandbox start)."""
    keys: set[str] = set()
    for spec in specs:
        for h in harnesses(spec):
            kind, _, model = h.partition("@")
            if kind == "claude":
                keys.add("ANTHROPIC_API_KEY")
            elif kind == "codex":
                keys.add("OPENAI_API_KEY")
            elif kind == "sdk":
                if model.startswith("litellm/"):
                    provider = model.split("/")[1] if model.count("/") >= 2 else ""
                    mapped = {
                        "openrouter": "OPENROUTER_API_KEY",
                        "anthropic": "ANTHROPIC_API_KEY",
                        "openai": "OPENAI_API_KEY",
                        "gemini": "GEMINI_API_KEY",
                    }.get(provider)
                    if mapped:
                        keys.add(mapped)
                else:
                    keys.add("OPENAI_API_KEY")
    return tuple(sorted(keys))


def _usage_json(usage: Any) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
    }


def _run_json(run: Any, *, full: bool) -> dict[str, Any]:
    data = {
        "harness": run.harness,
        "model": run.model,
        "role": run.role,
        "exit_code": run.exit_code,
        "duration_s": round(run.duration_s, 3),
        "session_id": run.session_id,
        "usage": _usage_json(run.usage),
        "final_message": run.final_message if full else run.final_message[:FINAL_MESSAGE_CHARS],
    }
    if full:
        data["log"] = run.log
    return data


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn final line from an interrupted run
    return records


@dataclass
class RunSummary:
    attempted: int = 0
    skipped_finished: int = 0
    skipped_budget: int = 0
    errored: int = 0
    resolved: int = 0
    spent_usd: float = 0.0
    unpriced_pairs: int = 0
    halted: Optional[str] = None
    records: list[dict[str, Any]] = field(default_factory=list)


class _Ledger:
    """Append-only JSONL results plus the running budget state."""

    def __init__(self, out: Path, max_cost_usd: Optional[float]) -> None:
        self.out = out
        self.path = out / RESULTS_FILE
        self.max_cost_usd = max_cost_usd
        self._lock = asyncio.Lock()
        self.records = load_records(self.path)
        # Latest record per pair wins (a retried error is superseded by its retry).
        self.latest: dict[tuple[str, str], dict[str, Any]] = {
            (r["task_id"], r["team"]): r for r in self.records
        }
        self.spent = sum(
            (r.get("usage") or {}).get("cost_usd") or 0.0 for r in self.latest.values()
        )
        self.halted: Optional[str] = None

    def over_budget(self) -> bool:
        return self.max_cost_usd is not None and self.spent >= self.max_cost_usd

    async def append(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self.out.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
                handle.flush()
            prev = self.latest.get((record["task_id"], record["team"]))
            if prev is not None:
                self.spent -= (prev.get("usage") or {}).get("cost_usd") or 0.0
            self.latest[(record["task_id"], record["team"])] = record
            cost = (record.get("usage") or {}).get("cost_usd")
            self.spent += cost or 0.0
            if self.max_cost_usd is not None and record.get("usage") and cost is None:
                self.halted = (
                    f"pair {record['task_id']} x {record['team']} reported an unpriced cost; "
                    "the budget cannot be enforced"
                )


def _write_artifacts(
    out: Path, team: str, task_id: str, patch: str, result: Optional[TeamResult], log: str
) -> None:
    directory = out / ARTIFACTS_DIR / slug(team) / slug(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "patch.diff").write_text(patch, encoding="utf-8", errors="surrogateescape")
    if result is not None:
        (directory / "runs.json").write_text(
            json.dumps(
                {"meta": result.meta, "runs": [_run_json(r, full=True) for r in result.runs]},
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
    if log:
        (directory / "eval.log").write_text(log, encoding="utf-8", errors="replace")


async def run_benchmark(
    tasks: Sequence[BenchTask],
    team_specs: Sequence[str],
    out_dir: str | Path,
    *,
    concurrency: int = 4,
    max_cost_usd: Optional[float] = None,
    allow_unpriced: bool = False,
    solve_timeout_s: Optional[float] = 3600.0,
    retry_errors: bool = True,
    team_factory: TeamFactory = default_team_factory,
    workspace_factory: Optional[WorkspaceFactory] = None,
    evaluator: Optional[Evaluator] = None,
    unpriced_fn: UnpricedFn = default_unpriced,
    log: Callable[[str], None] = print,
    run_meta: Optional[dict[str, Any]] = None,
    sandbox_config: Optional[SandboxConfig] = None,
) -> RunSummary:
    out = Path(out_dir)
    if not team_specs:
        raise ValueError("at least one team spec is required")

    if max_cost_usd is not None and not allow_unpriced:
        unpriced = {spec: bad for spec in team_specs if (bad := unpriced_fn(spec))}
        if unpriced:
            detail = "; ".join(f"{s}: {', '.join(b)}" for s, b in unpriced.items())
            raise BudgetError(
                f"cannot enforce --max-cost-usd: unpriced harness in {detail}. "
                "Pin a priced model (harness@model) or pass --allow-unpriced."
            )

    # Canonical names (the runner validates the spec); fail before any sandbox exists.
    names: dict[str, str] = {}
    for spec in team_specs:
        name = team_factory(spec).name
        if name in names.values():
            raise ValueError(f"duplicate team after canonicalisation: {name}")
        names[spec] = name

    if workspace_factory is None or evaluator is None:
        backend = ModalBackend(sandbox_config)
        if workspace_factory is None:

            async def workspace_factory(task: BenchTask) -> Workspace:  # noqa: F811
                return await ModalSandboxWorkspace.create(task.image, backend=backend, agent=True)

        if evaluator is None:

            async def evaluator(task: BenchTask, patch: str) -> EvalResult:  # noqa: F811
                return await evaluate_patch(task, patch, backend=backend)

    ledger = _Ledger(out, max_cost_usd)
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "teams": list(names.values()),
        "task_ids": [t.task_id for t in tasks],
        "max_cost_usd": max_cost_usd,
        "datasets": {k: {"repo_id": s.repo_id, "revision": s.revision} for k, s in SOURCES.items()},
        **(run_meta or {}),
    }
    (out / META_FILE).write_text(json.dumps(meta, indent=1), encoding="utf-8")

    summary = RunSummary()
    pairs = [(task, spec) for task in tasks for spec in team_specs]

    async def run_pair(pair: tuple[BenchTask, str]) -> None:
        task, spec = pair
        name = names[spec]
        prev = ledger.latest.get((task.task_id, name))
        if prev is not None and prev.get("error") is None:
            summary.skipped_finished += 1
            return
        if prev is not None and not retry_errors:
            summary.skipped_finished += 1
            return
        if ledger.halted or ledger.over_budget():
            summary.skipped_budget += 1
            return
        summary.attempted += 1
        record = await _run_pair(
            task, spec, name, prev, out, solve_timeout_s, team_factory, workspace_factory, evaluator
        )
        await ledger.append(record)
        summary.records.append(record)
        if record.get("error"):
            summary.errored += 1
        elif record.get("resolved"):
            summary.resolved += 1
        label = "ERROR " + record["error"][:120] if record.get("error") else (
            "resolved" if record.get("resolved") else "unresolved"
        )
        log(f"[{summary.attempted}] {task.task_id} x {name}: {label} (spent ${ledger.spent:.2f})")

    await map_concurrently(pairs, run_pair, max_concurrency=concurrency)

    summary.spent_usd = ledger.spent
    summary.halted = ledger.halted or ("budget reached" if ledger.over_budget() and summary.skipped_budget else None)
    summary.unpriced_pairs = sum(
        1 for r in ledger.latest.values() if r.get("usage") and r["usage"].get("cost_usd") is None
    )
    return summary


async def _run_pair(
    task: BenchTask,
    spec: str,
    name: str,
    prev: Optional[dict[str, Any]],
    out: Path,
    solve_timeout_s: Optional[float],
    team_factory: TeamFactory,
    workspace_factory: WorkspaceFactory,
    evaluator: Evaluator,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    started = time.time()
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "team": name,
        "dataset": task.dataset,
        "repo": task.repo,
        "language": task.language,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "resolved": None,
        "patch": "",
        "usage": None,
        "duration_s": None,
        "runs": [],
        "meta": {},
        "eval": None,
        "error": None,
        "stage": "solve",
    }
    result: Optional[TeamResult] = None
    if prev is not None and prev.get("stage") == "evaluate" and prev.get("patch"):
        # Only grading failed last time: keep the agents' work and cost, re-grade the patch.
        for key in ("patch", "usage", "duration_s", "runs", "meta"):
            record[key] = prev[key]
        record["resumed_from_error"] = prev["error"]
    else:
        try:
            team = team_factory(spec)
            workspace = await workspace_factory(task)
            try:
                t0 = loop.time()
                coro = team.solve(task.coding_task(), workspace)
                result = await (
                    asyncio.wait_for(coro, solve_timeout_s) if solve_timeout_s else coro
                )
                wall = loop.time() - t0
            finally:
                await workspace.close()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            if isinstance(exc, SandboxExecTimeout):  # a command timed out inside the team
                record["error"] = f"solve: SandboxExecTimeout: {exc}"[:2000]
            else:
                record["error"] = f"solve timed out after {solve_timeout_s:g}s"
            return record
        except Exception as exc:
            record["error"] = f"solve: {type(exc).__name__}: {exc}"[:2000]
            return record
        record.update(
            patch=result.patch,
            usage=_usage_json(result.usage),
            duration_s=round(result.duration_s or wall, 3),
            runs=[_run_json(r, full=False) for r in result.runs],
            meta=result.meta,
        )

    record["stage"] = "evaluate"
    try:
        evaluation = await evaluator(task, record["patch"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        record["error"] = f"evaluate: {type(exc).__name__}: {exc}"[:2000]
        _write_artifacts(out, name, task.task_id, record["patch"], result, "")
        return record
    record["resolved"] = evaluation.resolved
    record["eval"] = evaluation.to_json()
    record["stage"] = "done"
    _write_artifacts(out, name, task.task_id, record["patch"], result, evaluation.log)
    return record


GOLD_FILE = "gold.jsonl"


async def validate_gold(
    tasks: Sequence[BenchTask],
    out_dir: str | Path,
    *,
    concurrency: int = 4,
    evaluator: Optional[Evaluator] = None,
    log: Callable[[str], None] = print,
    sandbox_config: Optional[SandboxConfig] = None,
) -> list[str]:
    """Grade each task's reference patch; return ids where it resolves.

    Upstream recommends this before benchmarking: instances can rot (flaky or time-dependent
    tests, vanished dependencies), and results should use only instances whose gold patch resolves
    on the machine and day of the run. Already-graded ids in ``gold.jsonl`` are not repeated.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / GOLD_FILE
    done = {r["task_id"]: r for r in load_records(path)}
    if evaluator is None:
        backend = ModalBackend(sandbox_config)

        async def evaluator(task: BenchTask, patch: str) -> EvalResult:  # noqa: F811
            return await evaluate_patch(task, patch, backend=backend)

    lock = asyncio.Lock()

    async def one(task: BenchTask) -> None:
        if task.task_id in done and done[task.task_id].get("error") is None:
            return
        record: dict[str, Any] = {"task_id": task.task_id, "resolved": False, "error": None}
        try:
            result = await evaluator(task, task.grading.gold_patch)
            record["resolved"] = result.resolved
            record["eval"] = result.to_json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        async with lock:
            done[task.task_id] = record
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        log(f"gold {task.task_id}: {'valid' if record['resolved'] else 'INVALID'}")

    await map_concurrently(list(tasks), one, max_concurrency=concurrency)
    return [t.task_id for t in tasks if done.get(t.task_id, {}).get("resolved")]

"""Benchmark orchestration: task x team pairs -> sandbox -> team -> patch -> hidden tests -> JSONL.

``results.jsonl`` in the output directory holds one record per (task, team) pair. The run is
resumable: pairs with a finished record are skipped; pairs that errored are retried (an error in the
evaluation stage re-grades the saved patch instead of paying for the agents again).

What counts as an error. Only infrastructure failures: the workspace could not be created
(``stage="workspace"``, no agent cost incurred), the workspace diff could not be read after the team
stopped, or grading itself failed. A team that runs out of time (``--solve-timeout``) or crashes is
*graded on whatever it left in the workspace* and recorded with ``meta.timed_out`` /
``meta.solve_error``. Dropping those pairs would remove exactly the hardest tasks for the slowest
topologies and bias every paired comparison.

Budget guard (``max_cost_usd``): before each pair starts, total priced spend so far (including
previous runs in the same output directory) is compared with the budget and no new pair starts once
it is reached. Pairs already running finish, so the final spend can exceed the budget by up to
``concurrency`` pairs. A budgeted run refuses to start when a team contains a harness whose cost
cannot be priced unless ``allow_unpriced`` is set. Without ``allow_unpriced`` the run also stops
launching as soon as any record has an unknown cost, which includes every timed-out or crashed
solve: the agents may have spent money the run cannot see, so the budget can no longer be enforced.
With ``allow_unpriced`` the budget counts priced spend only.

Everything written to disk is passed through a ``Redactor`` first; grading uses the originals.
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
from agent.teams.validation import validate_positive_finite
from agent.workflows.concurrency import map_concurrently

from .dataset import SOURCES, BenchTask
from .evaluate import EvalResult, evaluate_patch
from .redact import Redactor
from .sandbox import ModalBackend, SandboxBackend, SandboxWorkspace

RESULTS_FILE = "results.jsonl"
META_FILE = "meta.json"
ARTIFACTS_DIR = "artifacts"
GOLD_FILE = "gold.jsonl"
FINAL_MESSAGE_CHARS = 1000

TeamFactory = Callable[[str], Team]
WorkspaceFactory = Callable[[BenchTask], Awaitable[Workspace]]
Evaluator = Callable[[BenchTask, str], Awaitable[EvalResult]]
UnpricedFn = Callable[[str], list[str]]

UNKNOWN_USAGE = {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": None,
}


class BudgetError(RuntimeError):
    """The run cannot honour ``--max-cost-usd`` (unpriced harness in a budgeted run)."""


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+=@,-]+", "_", text).strip("_") or "x"


def default_team_factory(spec: str) -> Team:
    from agent.teams import build_team  # lazy: keeps agent.bench importable on its own

    # The sandbox is the isolation boundary, so CLIs run with permissions bypassed.
    return build_team(spec, permissions="bypass")


def harness_specs(spec: str) -> list[str]:
    """Every harness (``harness[@model]``) a team spec uses, via the runner's own parser."""
    from agent.teams import parse_team_spec

    return [str(h) for h in parse_team_spec(spec).harness_specs]


def canonical_team(spec: str) -> str:
    from agent.teams import parse_team_spec

    return parse_team_spec(spec).canonical


def default_unpriced(spec: str) -> list[str]:
    from agent.teams import is_priced

    return [h for h in harness_specs(spec) if not is_priced(h)]


def required_secret_keys(
    specs: Sequence[str], harnesses: Callable[[str], list[str]] = harness_specs
) -> tuple[str, ...]:
    """Keys the agent sandbox needs for the CLIs that run *inside* it (Modal Secret or host env).

    ``codex exec`` without a saved login authenticates only with ``CODEX_API_KEY``; it ignores
    ``OPENAI_API_KEY``. ``sdk`` harnesses make their model calls from the host process, so their
    provider keys are neither required in the secret nor made readable to the agent's shell.
    """
    keys: set[str] = set()
    for spec in specs:
        for h in harnesses(spec):
            kind = h.partition("@")[0]
            if kind == "claude":
                keys.add("ANTHROPIC_API_KEY")
            elif kind == "codex":
                keys.add("CODEX_API_KEY")
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


def load_records(path: Path, *, repair_tail: bool = False) -> list[dict[str, Any]]:
    """Read JSONL, tolerating only a torn final line (no trailing newline) from an interrupted write.

    Writers emit each record and its newline in one write, so any unterminated line that does not
    parse is a torn tail; a newline-terminated line that does not parse is corruption and raises.
    With ``repair_tail`` (writers about to append) the torn tail is truncated, or a valid final
    record gets its missing newline. Read-only consumers leave the file untouched.
    """
    if not path.exists():
        return []
    records = []
    with path.open("r+b" if repair_tail else "rb") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    if line.endswith(b"\n"):
                        raise ValueError(f"{path}:{line_number}: {exc.msg}") from exc
                    if repair_tail:
                        handle.seek(-len(line), 1)
                        handle.truncate()
                    break
            if repair_tail and not line.endswith(b"\n"):
                handle.write(b"\n")
    return records


def load_meta(out: Path) -> dict[str, Any]:
    path = out / META_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record_run_meta(out: Path, entry: dict[str, Any]) -> None:
    """Append this invocation to ``meta.json``; ``created_at`` and earlier runs are preserved."""
    meta = load_meta(out)
    meta.setdefault("created_at", entry["started_at"])
    meta["datasets"] = {
        k: {"repo_id": s.repo_id, "revision": s.revision} for k, s in SOURCES.items()
    }
    meta.setdefault("runs", []).append(entry)
    (out / META_FILE).write_text(json.dumps(meta, indent=1), encoding="utf-8")


@dataclass
class RunSummary:
    attempted: int = 0
    skipped_finished: int = 0
    skipped_budget: int = 0
    errored: int = 0
    resolved: int = 0
    timed_out: int = 0
    spent_usd: float = 0.0
    unpriced_pairs: int = 0
    halted: Optional[str] = None
    records: list[dict[str, Any]] = field(default_factory=list)


def _cost(record: dict[str, Any]) -> Optional[float]:
    return (record.get("usage") or {}).get("cost_usd")


class _Ledger:
    """Append-only JSONL results plus the running budget state."""

    def __init__(
        self, out: Path, max_cost_usd: Optional[float], allow_unpriced: bool, redactor: Redactor
    ) -> None:
        self.out = out
        self.path = out / RESULTS_FILE
        self.max_cost_usd = max_cost_usd
        self.allow_unpriced = allow_unpriced
        self.redactor = redactor
        self._lock = asyncio.Lock()
        self.records = load_records(self.path, repair_tail=True)
        # Latest record per pair wins (a retried error is superseded by its retry).
        self.latest: dict[tuple[str, str], dict[str, Any]] = {
            (r["task_id"], r["team"]): r for r in self.records
        }
        self.spent = sum(_cost(r) or 0.0 for r in self.latest.values())
        self.halted: Optional[str] = None
        for r in self.latest.values():
            self._check_unpriced(r)

    def _check_unpriced(self, record: dict[str, Any]) -> None:
        if (
            self.max_cost_usd is not None
            and not self.allow_unpriced
            and record.get("usage")
            and _cost(record) is None
            and self.halted is None
        ):
            self.halted = (
                f"pair {record['task_id']} x {record['team']} has an unknown cost (unpriced "
                "harness, timeout or crash); the budget cannot be enforced "
                "(--allow-unpriced counts priced spend only)"
            )

    def over_budget(self) -> bool:
        return self.max_cost_usd is not None and self.spent >= self.max_cost_usd

    async def append(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = self.redactor.obj(record)
        stored["redacted"] = stored != record
        stored["patch_redacted"] = stored.get("patch") != record.get("patch")
        async with self._lock:
            self.out.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored, ensure_ascii=True) + "\n")
                handle.flush()
            key = (stored["task_id"], stored["team"])
            prev = self.latest.get(key)
            if prev is not None:
                self.spent -= _cost(prev) or 0.0
            self.latest[key] = stored
            self.spent += _cost(stored) or 0.0
            self._check_unpriced(stored)
        return stored


def _write_artifacts(
    out: Path,
    redactor: Redactor,
    team: str,
    task_id: str,
    patch: str,
    result: Optional[TeamResult],
    log: str,
) -> None:
    directory = out / ARTIFACTS_DIR / slug(team) / slug(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "patch.diff").write_text(
        redactor.text(patch), encoding="utf-8", errors="surrogateescape"
    )
    if result is not None:
        payload = {"meta": result.meta, "runs": [_run_json(r, full=True) for r in result.runs]}
        (directory / "runs.json").write_text(
            json.dumps(redactor.obj(json.loads(json.dumps(payload, default=str))), indent=1),
            encoding="utf-8",
        )
    if log:
        (directory / "eval.log").write_text(redactor.text(log), encoding="utf-8", errors="replace")


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
    backend: Optional[SandboxBackend] = None,
    redactor: Optional[Redactor] = None,
) -> RunSummary:
    if max_cost_usd is not None:
        validate_positive_finite(max_cost_usd, "max_cost_usd")
    if solve_timeout_s is not None:
        validate_positive_finite(solve_timeout_s, "solve_timeout_s")
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
        backend = backend or ModalBackend()
        redactor = redactor or backend.redactor
        if workspace_factory is None:

            async def workspace_factory(task: BenchTask) -> Workspace:  # noqa: F811
                return await SandboxWorkspace.create(
                    task.image, backend=backend, agent=True, expected_commit=task.base_commit
                )

        if evaluator is None:

            async def evaluator(task: BenchTask, patch: str) -> EvalResult:  # noqa: F811
                return await evaluate_patch(task, patch, backend=backend)

    redactor = redactor or Redactor()
    ledger = _Ledger(out, max_cost_usd, allow_unpriced, redactor)
    out.mkdir(parents=True, exist_ok=True)
    record_run_meta(
        out,
        {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "teams": list(names.values()),
            "task_ids": [t.task_id for t in tasks],
            "max_cost_usd": max_cost_usd,
            "allow_unpriced": allow_unpriced,
            "concurrency": concurrency,
            "solve_timeout_s": solve_timeout_s,
            **(run_meta or {}),
        },
    )

    summary = RunSummary()
    pairs = [(task, spec) for task in tasks for spec in team_specs]

    async def run_pair(pair: tuple[BenchTask, str]) -> None:
        task, spec = pair
        name = names[spec]
        prev = ledger.latest.get((task.task_id, name))
        if prev is not None and (prev.get("error") is None or not retry_errors):
            summary.skipped_finished += 1
            return
        if ledger.halted or ledger.over_budget():
            summary.skipped_budget += 1
            return
        summary.attempted += 1
        record, result, eval_log = await _run_pair(
            task, spec, name, prev, solve_timeout_s, team_factory, workspace_factory, evaluator
        )
        stored = await ledger.append(record)
        if record["patch"] or result is not None:
            _write_artifacts(out, redactor, name, task.task_id, record["patch"], result, eval_log)
        summary.records.append(stored)
        if stored.get("error"):
            summary.errored += 1
        elif stored.get("resolved"):
            summary.resolved += 1
        if (stored.get("meta") or {}).get("timed_out"):
            summary.timed_out += 1
        label = (
            "ERROR " + stored["error"][:120]
            if stored.get("error")
            else ("resolved" if stored.get("resolved") else "unresolved")
        )
        if (stored.get("meta") or {}).get("timed_out"):
            label += " (timed out, graded as left)"
        log(f"[{summary.attempted}] {task.task_id} x {name}: {label} (spent ${ledger.spent:.2f})")

    await map_concurrently(pairs, run_pair, max_concurrency=concurrency)

    summary.spent_usd = ledger.spent
    summary.halted = ledger.halted or (
        "budget reached" if ledger.over_budget() and summary.skipped_budget else None
    )
    summary.unpriced_pairs = sum(
        1 for r in ledger.latest.values() if r.get("usage") and _cost(r) is None
    )
    return summary


async def _solve(
    team: Team, task: BenchTask, workspace: Workspace, timeout_s: Optional[float]
) -> tuple[Optional[TeamResult], Optional[BaseException], bool]:
    """Run ``team.solve``; returns (result, crash, timed_out). Cancellation propagates, and takes
    the running solve down with it."""
    job = asyncio.ensure_future(team.solve(task.coding_task(), workspace))
    try:
        done, _ = await asyncio.wait({job}, timeout=timeout_s)
    except BaseException:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        raise
    if not done:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        return None, None, True
    try:
        return job.result(), None, False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, exc, False


async def _run_pair(
    task: BenchTask,
    spec: str,
    name: str,
    prev: Optional[dict[str, Any]],
    solve_timeout_s: Optional[float],
    team_factory: TeamFactory,
    workspace_factory: WorkspaceFactory,
    evaluator: Evaluator,
) -> tuple[dict[str, Any], Optional[TeamResult], str]:
    loop = asyncio.get_running_loop()
    started = time.time()
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "team": name,
        "dataset": task.dataset,
        "repo": task.repo,
        "language": task.language,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "base_commit": task.base_commit,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "resolved": None,
        "patch": "",
        "usage": None,
        "duration_s": None,
        "runs": [],
        "meta": {},
        "eval": None,
        "error": None,
        "stage": "workspace",
    }
    result: Optional[TeamResult] = None
    if (
        prev is not None
        and prev.get("stage") == "evaluate"
        and prev.get("patch")
        and not prev.get("patch_redacted")
    ):
        # Only grading failed last time: keep the agents' work and cost, re-grade the patch.
        for key in ("patch", "usage", "duration_s", "runs", "meta", "isolate_history", "image_head"):
            record[key] = prev.get(key, record.get(key))
        record["resumed_from_error"] = prev["error"]
    else:
        try:
            team = team_factory(spec)
            workspace = await workspace_factory(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No agent has run yet: nothing was spent, usage stays None.
            record["error"] = f"workspace: {type(exc).__name__}: {exc}"[:2000]
            return record, None, ""
        record["stage"] = "solve"
        record["isolate_history"] = getattr(workspace, "isolate_history", None)
        record["image_head"] = getattr(workspace, "head", None)
        try:
            t0 = loop.time()
            result, crash, timed_out = await _solve(team, task, workspace, solve_timeout_s)
            wall = loop.time() - t0
            if result is not None:
                patch, meta = result.patch, dict(result.meta)
                usage = _usage_json(result.usage)
                duration = result.duration_s or wall
                runs = [_run_json(r, full=False) for r in result.runs]
            else:
                # Out of time or crashed: grade whatever the team left in the workspace. Its cost
                # is unknown, which a budgeted run treats as unpriced.
                try:
                    patch = await workspace.diff()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    record["error"] = (
                        f"solve: could not read the workspace after "
                        f"{'a timeout' if timed_out else type(crash).__name__}: "
                        f"{type(exc).__name__}: {exc}"
                    )[:2000]
                    record["usage"] = dict(UNKNOWN_USAGE)
                    record["duration_s"] = round(wall, 3)
                    return record, None, ""
                if timed_out:
                    meta = {"timed_out": True, "solve_timeout_s": solve_timeout_s}
                else:
                    meta = {"solve_error": f"{type(crash).__name__}: {crash}"[:1000]}
                usage = dict(UNKNOWN_USAGE)
                duration = wall
                runs = []
        finally:
            await workspace.close()
        record.update(
            patch=patch, usage=usage, duration_s=round(duration, 3), runs=runs, meta=meta
        )

    record["stage"] = "evaluate"
    try:
        evaluation = await evaluator(task, record["patch"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        record["error"] = f"evaluate: {type(exc).__name__}: {exc}"[:2000]
        return record, result, ""
    record["resolved"] = evaluation.resolved
    record["eval"] = evaluation.to_json()
    record["stage"] = "done"
    return record, result, evaluation.log


async def validate_gold(
    tasks: Sequence[BenchTask],
    out_dir: str | Path,
    *,
    concurrency: int = 4,
    evaluator: Optional[Evaluator] = None,
    log: Callable[[str], None] = print,
    backend: Optional[SandboxBackend] = None,
) -> list[str]:
    """Grade each task's reference patch; return ids where it resolves.

    Upstream recommends this before benchmarking: instances can rot (flaky or time-dependent
    tests, vanished dependencies), and results should use only instances whose gold patch resolves
    on the machine and day of the run. Already-graded ids in ``gold.jsonl`` are not repeated.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / GOLD_FILE
    done = {r["task_id"]: r for r in load_records(path, repair_tail=True)}
    if evaluator is None:
        backend = backend or ModalBackend()

        async def evaluator(task: BenchTask, patch: str) -> EvalResult:  # noqa: F811
            return await evaluate_patch(task, patch, backend=backend)

    redactor = Redactor()
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
        record = redactor.obj(record)
        async with lock:
            done[task.task_id] = record
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        log(f"gold {task.task_id}: {'valid' if record['resolved'] else 'INVALID'}")

    await map_concurrently(list(tasks), one, max_concurrency=concurrency)
    return [t.task_id for t in tasks if done.get(t.task_id, {}).get("resolved")]

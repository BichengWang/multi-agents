"""``python -m agent.bench`` command line: tasks | gold | run | grade-candidates | report | remote."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from agent.teams.validation import validate_positive_finite

from .candidates import CANDIDATES_FILE, grade_candidates
from .dataset import DEFAULT_DATASET, SOURCES, BenchTask, load_tasks
from .evaluate import EvalResult, evaluate_patch
from .report import MODAL_SANDBOX_USD_PER_HOUR, build_report, latest_run
from .run import (
    GOLD_FILE,
    RESULTS_FILE,
    BudgetError,
    canonical_team,
    load_meta,
    load_records,
    required_secret_keys,
    run_benchmark,
    validate_gold,
)
from .sandbox import SECRET_HELP, SandboxBackend, SandboxConfig, SandboxError, make_backend


def _add_task_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"one of {sorted(SOURCES)} or a local .jsonl of dataset rows (default: %(default)s)",
    )
    p.add_argument("--since", help="only tasks created on/after this date (YYYY-MM-DD, UTC)")
    p.add_argument("--until", help="only tasks created before this date (YYYY-MM-DD, UTC)")
    p.add_argument("--repo", action="append", dest="repos", help="owner/name; repeatable")
    p.add_argument(
        "--language", action="append", dest="languages", help="e.g. go, rust, cpp; repeatable"
    )
    p.add_argument("--instance-id", action="append", dest="instance_ids", help="repeatable")
    p.add_argument("--limit", type=int, help="seeded random sample of this many tasks")
    p.add_argument("--seed", type=int, default=0, help="sampling seed (default: %(default)s)")
    p.add_argument(
        "--gold-file",
        help="gold.jsonl from `gold`; keep only tasks whose reference patch resolved",
    )


def _add_sandbox_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--backend",
        choices=("modal", "docker"),
        default="modal",
        help="where sandboxes run: Modal, or the local Docker daemon with API keys from your "
        "environment (default: %(default)s)",
    )
    p.add_argument("--app-name", default=SandboxConfig.app_name, help="Modal app (default: %(default)s)")
    p.add_argument(
        "--secret",
        default=SandboxConfig.secret_name,
        help="Modal Secret holding the API keys (default: %(default)s)",
    )
    p.add_argument(
        "--registry-secret",
        help="Modal Secret with REGISTRY_USERNAME/REGISTRY_PASSWORD for Docker Hub pulls "
        "(docker backend: use `docker login`)",
    )
    p.add_argument("--add-python", help="Modal only: Python series to inject into images without Python")
    p.add_argument("--concurrency", type=int, default=4, help="parallel pairs (default: %(default)s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent.bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    tasks = sub.add_parser("tasks", help="print the (seeded) task sample; needs no credentials")
    _add_task_filters(tasks)
    tasks.add_argument("--json", action="store_true", help="one JSON object per line")

    gold = sub.add_parser("gold", help="check which tasks' reference patch still resolves (sandboxes)")
    _add_task_filters(gold)
    _add_sandbox_options(gold)
    gold.add_argument("--out", required=True, help="directory; writes gold.jsonl")

    run = sub.add_parser("run", help="run team specs on fresh tasks and grade them (sandboxes)")
    _add_task_filters(run)
    _add_sandbox_options(run)
    run.add_argument("--team", action="append", required=True, help="team spec; repeatable")
    run.add_argument("--out", required=True, help="output directory (resumable)")
    run.add_argument("--max-cost-usd", type=float, help="stop launching pairs once priced spend reaches this")
    run.add_argument(
        "--allow-unpriced",
        action="store_true",
        help="allow --max-cost-usd with harnesses whose cost cannot be priced",
    )
    run.add_argument(
        "--solve-timeout",
        type=float,
        default=3600.0,
        help="seconds per team solve before the pair is recorded as errored (default: %(default)s)",
    )
    run.add_argument(
        "--no-retry-errors", action="store_true", help="on resume, do not retry errored pairs"
    )
    run.add_argument(
        "--no-check-base-commit",
        action="store_true",
        help="do not require the image checkout to equal the task's base_commit",
    )
    run.add_argument(
        "--keep-history",
        action="store_true",
        help="do not strip git history in agent sandboxes (the fix commit may be readable)",
    )

    grade = sub.add_parser(
        "grade-candidates",
        help="grade the best-of-N candidate and review-round patches stored in a run directory "
        "with the hidden tests (grading sandboxes only, no agents); writes candidates.jsonl",
    )
    grade.add_argument("dir", help="a `run` output directory")
    grade.add_argument(
        "--dataset", help="dataset of the tasks (default: the one in the directory's meta.json)"
    )
    grade.add_argument("--team", action="append", help="only this team (repeatable); default: all")
    _add_sandbox_options(grade)
    # Taken from the run's meta.json so candidates are graded like the submitted patches.
    grade.set_defaults(backend=None)

    report = sub.add_parser("report", help="Markdown report of a run directory")
    report.add_argument("dir")
    report.add_argument("--baseline", help="team spec to compare every team against")
    report.add_argument(
        "--team",
        action="append",
        help="only report this team (repeatable); default: the teams of the latest run",
    )
    report.add_argument(
        "--count-errors-as-failures",
        action="store_true",
        help="count errored pairs as unresolved instead of excluding them",
    )
    report.add_argument(
        "--sandbox-usd-per-hour",
        type=float,
        default=MODAL_SANDBOX_USD_PER_HOUR,
        help="price sandbox-hours at this rate, stated in the report "
        f"(default: Modal's list price for the runner's sandbox, {MODAL_SANDBOX_USD_PER_HOUR:g})",
    )
    report.add_argument("--out", help="also write the report to this file")

    remote = sub.add_parser(
        "remote",
        help="run `gold ...`, `run ...` or `grade-candidates ...` as a detached Modal function "
        "(survives this machine sleeping); relative paths are in the Modal Volume "
        "multi-agents-bench-runs",
    )
    remote.add_argument(
        "argv", nargs=argparse.REMAINDER, help="a gold, run or grade-candidates command line"
    )
    return parser


def _select(args: argparse.Namespace) -> list[BenchTask]:
    tasks = load_tasks(
        args.dataset,
        since=args.since,
        until=args.until,
        repos=args.repos,
        languages=args.languages,
        instance_ids=args.instance_ids,
        # With a gold filter the sample is drawn from valid tasks only (see below).
        limit=None if args.gold_file else args.limit,
        seed=args.seed,
    )
    if args.gold_file:
        valid = {r["task_id"] for r in load_records(Path(args.gold_file)) if r.get("resolved")}
        tasks = [t for t in tasks if t.task_id in valid]
        if args.limit is not None and args.limit < len(tasks):
            import random

            chosen = set(
                t.task_id for t in random.Random(args.seed).sample(sorted(tasks, key=lambda t: t.task_id), args.limit)
            )
            tasks = [t for t in tasks if t.task_id in chosen]
    return tasks


def _backend(args: argparse.Namespace, teams: Sequence[str] = ()) -> SandboxBackend:
    if args.backend == "docker" and (args.registry_secret or args.add_python):
        raise ValueError("--registry-secret and --add-python apply to the modal backend only")
    keys = required_secret_keys(teams) if teams else ()
    config = SandboxConfig(
        app_name=args.app_name,
        secret_name=args.secret,
        secret_keys=keys,
        registry_secret_name=args.registry_secret,
        add_python=args.add_python,
        isolate_history=not getattr(args, "keep_history", False),
        check_base_commit=not getattr(args, "no_check_base_commit", False),
    )
    return make_backend(args.backend, config)


def _cmd_tasks(args: argparse.Namespace) -> int:
    tasks = _select(args)
    for t in tasks:
        row = {
            "task_id": t.task_id,
            "repo": t.repo,
            "language": t.language,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "base_commit": t.base_commit,
            "image": t.image,
            "f2p": len(t.grading.fail_to_pass),
            "p2p": len(t.grading.pass_to_pass),
        }
        if args.json:
            print(json.dumps(row))
        else:
            print(
                f"{t.task_id:<55} {t.language:<5} {row['created_at'][:10]}  "
                f"F2P={row['f2p']:<3} P2P={row['p2p']:<5} {t.image}"
            )
    print(f"{len(tasks)} task(s)", file=sys.stderr)
    return 0


def _cmd_gold(args: argparse.Namespace) -> int:
    args.gold_file = None  # validating gold must not depend on a previous gold file
    tasks = _select(args)
    valid = asyncio.run(
        validate_gold(tasks, args.out, concurrency=args.concurrency, backend=_backend(args))
    )
    print(f"{len(valid)}/{len(tasks)} tasks valid; see {Path(args.out) / GOLD_FILE}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.max_cost_usd is not None:
        validate_positive_finite(args.max_cost_usd, "--max-cost-usd")
    validate_positive_finite(args.solve_timeout, "--solve-timeout")
    tasks = _select(args)
    if not tasks:
        print("no tasks match the filters", file=sys.stderr)
        return 2
    print(f"{len(tasks)} task(s) x {len(args.team)} team(s)", file=sys.stderr)
    backend = _backend(args, args.team)
    try:
        summary = asyncio.run(
            run_benchmark(
                tasks,
                args.team,
                args.out,
                concurrency=args.concurrency,
                max_cost_usd=args.max_cost_usd,
                allow_unpriced=args.allow_unpriced,
                solve_timeout_s=args.solve_timeout,
                retry_errors=not args.no_retry_errors,
                backend=backend,
                run_meta={
                    "backend": args.backend,
                    "dataset": args.dataset,
                    "since": args.since,
                    "until": args.until,
                    "repos": args.repos,
                    "languages": args.languages,
                    "instance_ids": args.instance_ids,
                    "limit": args.limit,
                    "seed": args.seed,
                    "gold_file": args.gold_file,
                    # The grading image depends on these; grade-candidates reuses them.
                    "add_python": args.add_python,
                    "registry_secret": args.registry_secret,
                    "isolate_history": not args.keep_history,
                    "check_base_commit": not args.no_check_base_commit,
                },
            )
        )
    except BudgetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        modal_error = type(exc).__module__.startswith("modal")
        if modal_error and "secret" in str(exc).lower():
            print(f"error: {exc}\n{SECRET_HELP}", file=sys.stderr)
            return 2
        raise
    print(
        f"attempted {summary.attempted}, skipped (finished) {summary.skipped_finished}, "
        f"skipped (budget/halt) {summary.skipped_budget}, errored {summary.errored}, "
        f"timed out (graded) {summary.timed_out}, "
        f"spent ${summary.spent_usd:.2f}"
        + (f"; halted: {summary.halted}" if summary.halted else "")
    )
    return 0


# Options that change the grading sandbox; ``run`` records them in meta.json.
GRADING_ENV = {"backend": "--backend", "add_python": "--add-python", "registry_secret": "--registry-secret"}


def _grading_env(args: argparse.Namespace, recorded: dict[str, object]) -> None:
    """Grade in the run's own grading environment: take each ``GRADING_ENV`` option from the run's
    meta.json entry, and refuse a flag that contradicts it. Runs recorded before these were stored
    (the pilot) keep the flags given here."""
    for key, flag in GRADING_ENV.items():
        given = getattr(args, key)
        if key in recorded:
            if given is not None and given != recorded[key]:
                raise ValueError(
                    f"{flag} {given} contradicts the run's {recorded[key]!r} in meta.json; "
                    "grade with the run's settings"
                )
            setattr(args, key, recorded[key])
    if args.backend is None:
        args.backend = "modal"


def _cmd_grade_candidates(args: argparse.Namespace) -> int:
    out = Path(args.dir)
    records = load_records(out / RESULTS_FILE)
    if not records:
        raise ValueError(f"no results in {out / RESULTS_FILE}")
    run = latest_run(load_meta(out))
    _grading_env(args, run)
    dataset = args.dataset or run.get("dataset") or DEFAULT_DATASET
    tasks = load_tasks(dataset, instance_ids=sorted({r["task_id"] for r in records}))
    backend = _backend(args)

    async def evaluator(task: BenchTask, patch: str) -> EvalResult:
        return await evaluate_patch(task, patch, backend=backend)

    summary = asyncio.run(
        grade_candidates(
            out,
            tasks,
            evaluator=evaluator,
            concurrency=args.concurrency,
            teams=[canonical_team(t) for t in args.team] if args.team else None,
        )
    )
    print(
        f"graded {summary.graded} patch(es), {summary.resolved} resolved; errored {summary.errored}; "
        f"already graded {summary.already_graded}; see {out / CANDIDATES_FILE}"
        + (f"; not in {dataset}: {', '.join(summary.no_task)}" if summary.no_task else "")
    )
    return 0


def _canonicalize(spec: str) -> str:
    return canonical_team(spec)


def _cmd_report(args: argparse.Namespace) -> int:
    validate_positive_finite(args.sandbox_usd_per_hour, "--sandbox-usd-per-hour")
    text = build_report(
        args.dir,
        baseline=args.baseline,
        teams=args.team,
        count_errors_as_failures=args.count_errors_as_failures,
        canonicalize=_canonicalize,
        sandbox_usd_per_hour=args.sandbox_usd_per_hour,
    )
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


def _cmd_remote(args: argparse.Namespace) -> int:
    argv = list(args.argv)
    if not argv or argv[0] not in ("gold", "run", "grade-candidates"):
        raise ValueError("remote runs a `gold ...`, `run ...` or `grade-candidates ...` command")
    inner = build_parser().parse_args(argv)  # fail here, not after the remote start
    for flag in ("out", "gold_file", "dir"):
        value = getattr(inner, flag, None)
        if value and Path(value).is_absolute():
            name = "the directory" if flag == "dir" else f"--{flag.replace('_', '-')}"
            raise ValueError(f"{name} must be relative to the volume root")
    if (inner.backend or "modal") != "modal":  # grade-candidates may take it from meta.json
        raise ValueError("remote runs use the modal backend")
    from .remote import VOLUME, launch

    app_id, call_id = launch(argv)
    print(
        f"started {call_id} in app {app_id}\n"
        f"  follow:  modal app logs {app_id}\n"
        f"  fetch:   modal volume get {VOLUME} {getattr(inner, 'out', None) or inner.dir} <local dir>"
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {
            "tasks": _cmd_tasks,
            "gold": _cmd_gold,
            "run": _cmd_run,
            "grade-candidates": _cmd_grade_candidates,
            "report": _cmd_report,
            "remote": _cmd_remote,
        }[args.command](args)
    except (ValueError, SandboxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

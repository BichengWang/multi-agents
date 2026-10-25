"""``python -m agent run``: run a team on a task in throwaway git worktrees."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shlex
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agent.workflows import to_jsonable

from . import github
from .budget import Budget
from .harnesses.base import PERMISSIONS
from .spec import SpecError, build_team, is_priced, parse_team_spec
from .types import CodingTask, TeamResult
from .worktree import GitError, LocalWorktree, repo_key, run_process, user_cache_dir

EXIT_OK, EXIT_NO_PATCH, EXIT_USAGE, EXIT_APPLY, EXIT_FAILED, EXIT_CANCELLED = 0, 1, 2, 3, 4, 130
# Signals that cancel a run and clean up like Ctrl-C does; the exit code is 128 + the signal number
# (SIGINT 130, SIGTERM 143, SIGHUP 129).
CANCEL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
EXIT_PUBLISH = 3  # branch/push/PR failed; the patch is safe locally


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multi-agents", description="Run teams of coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="solve a task with a team in isolated git worktrees",
        description="Solve a task with a team of coding agents. The result is a patch; your "
        "working tree is never modified unless --apply is given.",
    )
    run.add_argument(
        "instruction",
        nargs="?",
        default=None,
        help="the task, in plain language (optional with --issue, where it is extra guidance)",
    )
    run.add_argument("--team", required=True, help="e.g. solo:worker=claude@haiku, "
                     "review:coder=codex,reviewer=claude,rounds=2, best:workers=claude+codex[,judge=H], "
                     "plan:planner=claude,workers=claude+codex[,integrator=H][,max_tasks=N][,replans=N]"
                     "[,reviewer=H][,rounds=N]")
    run.add_argument("--repo", default=".", help="git repository to work on (default: current directory)")
    run.add_argument("--base", default="HEAD", help="base ref the workers start from (default: HEAD)")
    run.add_argument("--timeout", type=float, default=None, help="seconds allowed per harness run")
    run.add_argument(
        "--check",
        default=None,
        metavar="CMD",
        help="shell command (tests, build, lint) run in a fork with the final patch applied; "
        "its pass/fail is recorded. It is also what reviewer=check / judge=check run",
    )
    run.add_argument("--keep-worktrees", action="store_true", help="do not delete the main worktree")
    run.add_argument(
        "--runs-dir",
        default=None,
        help="where run records are saved (default: <user cache>/multi-agents/runs/<repo>-<hash>, "
        "never inside your repository)",
    )
    run.add_argument(
        "--permissions",
        choices=PERMISSIONS,
        default="safe",
        help="safe: CLIs use their own sandbox (default); bypass: no permission checks, "
        "only for externally sandboxed machines",
    )
    run.add_argument("--apply", action="store_true", help="apply the patch if the working tree is clean")
    run.add_argument("--issue", metavar="N|URL", help="take the task from this GitHub issue (needs gh)")
    run.add_argument(
        "--pr",
        action="store_true",
        help="commit the patch to a new branch (your checkout is untouched), push it to origin and "
        "open a draft pull request (needs gh)",
    )
    run.add_argument(
        "--max-cost",
        type=float,
        default=None,
        metavar="USD",
        help="hard stop: no new harness run starts once this much has been spent (runs already "
        "in flight finish, so the total can exceed it). Needs priced harnesses unless "
        "--allow-unpriced",
    )
    run.add_argument(
        "--allow-unpriced",
        action="store_true",
        help="with --max-cost: accept harnesses that report no cost and count their runs as $0 "
        "(the limit then only covers the priced part)",
    )
    run.add_argument("--ready", action="store_true", help="with --pr: open a regular instead of a draft PR")
    return parser


def _files_changed(patch: str) -> list[tuple[str, int, int]]:
    files: list[tuple[str, int, int]] = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)$", line)
            files.append((match.group(2) if match else line[11:], 0, 0))
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and files and line[:1] in "+-":
            name, add, rem = files[-1]
            files[-1] = (name, add + (line[0] == "+"), rem + (line[0] == "-"))
    return files


def _cost_text(cost: Optional[float]) -> str:
    return "n/a" if cost is None else f"${cost:.4f}"


def _tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def _token_triplet(input_tokens: int, cached: int, output: int) -> str:
    return f"{_tokens(input_tokens)}/{_tokens(cached)}/{_tokens(output)}"


def render_summary(
    console: Console,
    team: str,
    result: TeamResult,
    *,
    repo: str,
    patch_path: Path,
    record_path: Path,
    budget: Optional[Budget] = None,
) -> None:
    console.print(f"Team: {team}", soft_wrap=True)
    table = Table(show_edge=False, pad_edge=False)
    table.add_column("role", no_wrap=True)
    table.add_column("harness@model", no_wrap=True, overflow="ellipsis")
    table.add_column("tokens in/cached/out", no_wrap=True, justify="right")
    table.add_column("cost", no_wrap=True, justify="right")
    table.add_column("secs", no_wrap=True, justify="right")
    for run in result.runs:
        u = run.usage
        failed = run.exit_code != 0
        table.add_row(
            f"[red]x {run.role}[/red]" if failed else run.role,
            f"{run.harness}@{run.model}" if run.model else run.harness,
            _token_triplet(u.input_tokens, u.cached_input_tokens, u.output_tokens),
            _cost_text(u.cost_usd),
            f"{run.duration_s:.1f}",
        )
    console.print(table)
    usage = result.usage
    if usage.cost_usd is not None:
        cost = _cost_text(usage.cost_usd)
    else:
        known = sum(r.usage.cost_usd for r in result.runs if r.usage.cost_usd is not None)
        unpriced = sum(1 for r in result.runs if r.usage.cost_usd is None)
        cost = f"${known:.4f} + {unpriced} unpriced run(s)"
    console.print(
        f"Total: {_token_triplet(usage.input_tokens, usage.cached_input_tokens, usage.output_tokens)} "
        f"tokens, cost {cost}, {result.duration_s:.1f}s",
        soft_wrap=True,
    )
    if budget is not None:
        line = f"budget: {_cost_text(budget.spent_usd)} of {_cost_text(budget.max_cost_usd)} used"
        if budget.unpriced_runs:
            line += f", {budget.unpriced_runs} unpriced run(s)"
        if budget.refused:
            line += f", {len(budget.refused)} run(s) refused"
        console.print(line, highlight=False, soft_wrap=True)
    for key in ("scores", "chosen", "best_round", "accepted"):
        if key in result.meta:
            console.print(f"{key}: {result.meta[key]}", soft_wrap=True)
    fallback = result.meta.get("fallback")
    if fallback:
        console.print(f"fallback: {fallback['to']} ({fallback['reason']})", markup=False, soft_wrap=True)
    for number, sub in enumerate(result.meta.get("subtasks", []), 1):
        status = f" [{sub['status']}]" if "status" in sub else ""
        round_note = f" (round {sub['round']})" if sub.get("round") else ""
        who = f" ({sub['worker']})" if "worker" in sub else ""
        console.print(
            f"subtask {number}{status}{round_note}: {sub['title']}{who}", markup=False, highlight=False, soft_wrap=True
        )
    for failure in result.meta.get("failures", []):
        console.print(f"[yellow]failure:[/yellow] {failure}", highlight=False, soft_wrap=True)
    check = result.meta.get("check")
    if check:
        status = "[green]passed[/green]" if check["passed"] else "[red]failed[/red]"
        code = f"exit {check['exit_code']}" if check["exit_code"] is not None else "did not finish"
        console.print(
            f"check: {status} ({code}, {check['duration_s']:.1f}s) {escape(check['command'])}",
            highlight=False,
            soft_wrap=True,
        )
        if not check["passed"] and check["output_tail"].strip():
            console.print(check["output_tail"].strip()[-600:], highlight=False, soft_wrap=True, markup=False)
    files = _files_changed(result.patch)
    if not files:
        console.print("[red]No changes were produced.[/red]")
    else:
        console.print(f"Files changed: {len(files)}")
        for name, add, rem in files:
            console.print(f"  {name}  [green]+{add}[/green] [red]-{rem}[/red]")
        console.print(f"\nRecord: {record_path}\nPatch:  {patch_path}", soft_wrap=True)
        console.print(
            f"Apply:  git -C {shlex.quote(repo)} apply {shlex.quote(str(patch_path))}",
            highlight=False,
            soft_wrap=True,
        )


def default_runs_dir(workspace: LocalWorktree) -> str:
    return str(user_cache_dir() / "runs" / repo_key(workspace.repo, workspace.common_dir))


NAME_MAX = 255  # bytes per path component on common file systems


def save_record(runs_dir: str, record: dict, patch: str) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    team = str(record["team"])
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", team).strip("-") or "run"
    room = NAME_MAX - len(stamp) - 1  # the slug is ASCII, so characters are bytes
    if len(slug) > room:
        # Long specs (many provider-qualified models) would exceed the file-name limit after the
        # paid work is done; keep a readable prefix and a hash of the full spec.
        digest = hashlib.sha256(team.encode("utf-8")).hexdigest()[:12]
        slug = f"{slug[:room - len(digest) - 1]}-{digest}"
    out = (Path(runs_dir) / f"{stamp}_{slug}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    record_path, patch_path = out / "record.json", out / "final.patch"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    patch_path.write_bytes(patch.encode("utf-8", "surrogateescape"))
    return record_path, patch_path


async def _is_clean(repo: str, *, untracked: bool) -> bool:
    flag = "--untracked-files=all" if untracked else "--untracked-files=no"
    code, out, _ = await run_process(["git", "status", "--porcelain", flag], cwd=repo)
    return code == 0 and not out.strip()


async def _apply_to_tree(repo: str, patch_path: Path, console: Console) -> int:
    # The tree may have changed while the team ran; untracked files only matter if they collide,
    # and then `git apply` refuses on its own.
    if not await _is_clean(repo, untracked=False):
        console.print("[red]Not applying: the working tree changed during the run.[/red]")
        return EXIT_APPLY
    code, _, err = await run_process(["git", "apply", "--binary", str(patch_path)], cwd=repo)
    if code != 0:
        console.print(f"[red]git apply failed:[/red] {err.decode('utf-8', 'replace').strip()}")
        return EXIT_APPLY
    console.print("Patch applied to your working tree (not committed).")
    return EXIT_OK


async def _open_pull_request(
    args: argparse.Namespace,
    console: Console,
    *,
    repo: str,
    patch_path: Path,
    instruction: str,
    team: str,
    result: TeamResult,
    issue: Optional[github.Issue],
    target: github.PrTarget,
    record: dict,
    record_path: Path,
) -> int:
    try:
        pub = await github.publish(
            repo,
            target=target,
            patch=result.patch,
            patch_path=patch_path,
            instruction=instruction,
            team=team,
            result=result,
            issue=issue,
            check=record.get("check"),
            draft=not args.ready,
            body_path=record_path.with_name("pr-body.md"),
        )
    except github.PublishError as exc:
        console.print(f"[red]Pull request not opened:[/red] {exc}", soft_wrap=True, highlight=False)
        if exc.branch:
            record["pull_request"] = {"url": None, "branch": exc.branch}
            record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            console.print(f"The commit is kept on local branch {exc.branch}. To finish by hand:")
        else:
            console.print("To finish by hand:")
        for command in exc.commands:
            console.print(f"  {command}", soft_wrap=True, highlight=False, markup=False)
        return EXIT_PUBLISH
    record["pull_request"] = pub.record()
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    console.print(f"Pull request: {pub.url} (branch {pub.branch})", soft_wrap=True, highlight=False)
    return EXIT_OK


async def run_command(args: argparse.Namespace, console: Console) -> int:
    if args.check is not None and not args.check.strip():
        console.print("[red]Invalid --check:[/red] the command is empty")
        return EXIT_USAGE
    if args.allow_unpriced and args.max_cost is None:
        console.print("[red]--allow-unpriced only applies together with --max-cost.[/red]")
        return EXIT_USAGE
    if args.max_cost is not None and not args.max_cost > 0:
        console.print(f"[red]Invalid --max-cost:[/red] must be a positive amount in USD, got {args.max_cost}")
        return EXIT_USAGE
    budget = None
    if args.max_cost is not None:
        budget = Budget(
            args.max_cost,
            allow_unpriced=args.allow_unpriced,
            on_warn=lambda b: console.print(
                f"[yellow]Budget: {_cost_text(b.spent_usd)} of {_cost_text(b.max_cost_usd)} used; "
                "no new run starts once it is used up.[/yellow]",
                highlight=False,
                soft_wrap=True,
            ),
        )
    try:
        spec = parse_team_spec(args.team)
        if budget is not None:
            unpriced = list(dict.fromkeys(str(h) for h in spec.harness_specs if not is_priced(str(h))))
            if unpriced and not args.allow_unpriced:
                console.print(
                    f"[red]--max-cost needs harnesses that report a cost; unpriced: {escape(', '.join(unpriced))}.[/red] "
                    "Pick priced harnesses (claude, or codex/sdk with a model listed in pricing.py) "
                    "or pass --allow-unpriced.",
                    soft_wrap=True,
                )
                return EXIT_USAGE
            if unpriced:
                console.print(
                    f"[yellow]Unpriced: {escape(', '.join(unpriced))}. Their runs count as $0 toward the "
                    "budget, so --max-cost only covers the priced part of the spend.[/yellow]",
                    soft_wrap=True,
                )
        team = build_team(
            args.team, permissions=args.permissions, timeout=args.timeout, check=args.check, budget=budget
        )
    except SpecError as exc:
        console.print(f"[red]Invalid --team:[/red] {exc}")
        return EXIT_USAGE
    if args.permissions == "safe" and any(h.harness == "sdk" for h in spec.harness_specs):
        console.print(
            "[yellow]The sdk harness has no sandbox: its shell commands run with your "
            "privileges in the worktree.[/yellow]"
        )
    if args.ready and not args.pr:
        console.print("[red]--ready only applies together with --pr.[/red]")
        return EXIT_USAGE
    if not args.instruction and not args.issue:
        console.print("[red]Give an instruction, --issue, or both.[/red]")
        return EXIT_USAGE
    issue = target = None
    try:
        if args.pr:
            target = await github.resolve_target(args.repo, args.base, await github.preflight(args.repo))
        if args.issue:
            issue = await github.read_issue(args.issue, args.repo)
    except (github.GitHubError, OSError) as exc:
        console.print(f"[red]GitHub:[/red] {exc}", soft_wrap=True, highlight=False)
        return EXIT_USAGE
    instruction = github.issue_instruction(issue, args.instruction) if issue else args.instruction
    try:
        workspace = await LocalWorktree.create(args.repo, target.base_commit if target else args.base)
    except (GitError, OSError) as exc:
        console.print(f"[red]Cannot create a worktree:[/red] {exc}")
        return EXIT_USAGE
    repo = workspace.repo
    if args.apply and not await _is_clean(repo, untracked=True):
        console.print("[red]--apply needs a clean working tree; commit or stash your changes first.[/red]")
        await workspace.close()
        return EXIT_APPLY
    runs_dir = args.runs_dir or default_runs_dir(workspace)
    task = CodingTask(task_id=datetime.now(timezone.utc).strftime("task-%Y%m%dT%H%M%S"), instruction=instruction)
    started_at = datetime.now(timezone.utc).isoformat()
    console.print(
        f"Team {team.name} on {repo} @ {workspace.base_commit[:12]} (worktree {workspace.root})",
        soft_wrap=True,
    )

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None

    received: list[int] = []

    def on_signal(signum: int) -> None:
        name = signal.Signals(signum).name
        console.print(f"\n[yellow]{name}: cancelling workers and cleaning up...[/yellow]")
        for sig in CANCEL_SIGNALS:  # a second signal force-quits
            loop.remove_signal_handler(sig)
        received.append(signum)
        main_task.cancel()

    for sig in CANCEL_SIGNALS:
        loop.add_signal_handler(sig, on_signal, sig)
    start = time.perf_counter()
    try:
        result = await team.solve(task, workspace)
    except asyncio.CancelledError:
        return 128 + received[0] if received else EXIT_CANCELLED
    except Exception as exc:
        console.print(f"[red]The team failed:[/red] {type(exc).__name__}: {exc}", soft_wrap=True)
        return EXIT_FAILED
    finally:
        for sig in CANCEL_SIGNALS:
            loop.remove_signal_handler(sig)
        if args.keep_worktrees:
            console.print(f"Worktree kept at {workspace.root}")
        else:
            await workspace.close()

    usage = result.usage
    record = {
        "team": team.name,
        "task": {"task_id": task.task_id, "instruction": task.instruction},
        "repo": repo,
        "base_ref": args.base,
        "base_commit": workspace.base_commit,
        "started_at": started_at,
        "duration_s": round(time.perf_counter() - start, 3),
        "patch": result.patch,
        "runs": to_jsonable(result.runs),
        "usage": to_jsonable(usage),
        "cost_usd": usage.cost_usd,
        "meta": to_jsonable(result.meta),
    }
    if issue:
        record["issue"] = issue.record()
    if "check" in result.meta:
        record["check"] = to_jsonable(result.meta["check"])
    if budget is not None:
        record["budget"] = budget.to_dict()
    record_path, patch_path = save_record(runs_dir, record, result.patch)
    render_summary(
        console, team.name, result, repo=repo, patch_path=patch_path, record_path=record_path, budget=budget
    )
    if issue:
        console.print(f"Issue: #{issue.number} {issue.title} ({issue.url})", soft_wrap=True, highlight=False)
    if not result.patch.strip():
        return EXIT_NO_PATCH
    code = EXIT_OK
    if args.pr:
        code = await _open_pull_request(
            args,
            console,
            repo=repo,
            patch_path=patch_path,
            instruction=instruction,
            team=team.name,
            result=result,
            issue=issue,
            target=target,
            record=record,
            record_path=record_path,
        )
    if args.apply:
        return await _apply_to_tree(repo, patch_path, console) or code
    return code


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    try:
        return asyncio.run(run_command(args, console))
    except KeyboardInterrupt:
        console.print("[red]Aborted.[/red]")
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())

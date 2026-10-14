"""``python -m agent run``: run a team on a task in throwaway git worktrees."""

from __future__ import annotations

import argparse
import asyncio
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
from rich.table import Table

from agent.workflows import to_jsonable

from .harnesses.base import PERMISSIONS
from .spec import SpecError, build_team, parse_team_spec
from .types import CodingTask, TeamResult
from .worktree import GitError, LocalWorktree, run_process

EXIT_OK, EXIT_NO_PATCH, EXIT_USAGE, EXIT_APPLY, EXIT_CANCELLED = 0, 1, 2, 3, 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent", description="Run teams of coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="solve a task with a team in isolated git worktrees",
        description="Solve a task with a team of coding agents. The result is a patch; your "
        "working tree is never modified unless --apply is given.",
    )
    run.add_argument("instruction", help="the task, in plain language")
    run.add_argument("--team", required=True, help="e.g. solo:worker=claude@haiku, "
                     "review:coder=codex,reviewer=claude,rounds=2, best:workers=claude+codex[,judge=H]")
    run.add_argument("--repo", default=".", help="git repository to work on (default: current directory)")
    run.add_argument("--base", default="HEAD", help="base ref the workers start from (default: HEAD)")
    run.add_argument("--timeout", type=float, default=None, help="seconds allowed per harness run")
    run.add_argument("--keep-worktrees", action="store_true", help="do not delete the main worktree")
    run.add_argument("--runs-dir", default="runs", help="where run records are saved (default: runs)")
    run.add_argument(
        "--permissions",
        choices=PERMISSIONS,
        default="safe",
        help="safe: CLIs use their own sandbox (default); bypass: no permission checks, "
        "only for externally sandboxed machines",
    )
    run.add_argument("--apply", action="store_true", help="apply the patch if the working tree is clean")
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


def render_summary(
    console: Console, team: str, result: TeamResult, *, repo: str, patch_path: Path, record_path: Path
) -> None:
    table = Table(title=f"Team {team}", title_justify="left")
    for col in ("#", "role", "harness", "model", "input", "cached", "output", "cost", "secs", "exit"):
        table.add_column(
            col,
            justify="right" if col in ("input", "cached", "output", "cost", "secs") else "left",
            no_wrap=True,
            overflow="fold",
        )
    for i, run in enumerate(result.runs, 1):
        u = run.usage
        table.add_row(
            str(i), run.role, run.harness, run.model or "-", f"{u.input_tokens:,}",
            f"{u.cached_input_tokens:,}", f"{u.output_tokens:,}", _cost_text(u.cost_usd),
            f"{run.duration_s:.1f}", str(run.exit_code),
        )
    console.print(table)
    usage = result.usage
    known = sum(r.usage.cost_usd for r in result.runs if r.usage.cost_usd is not None)
    unpriced = sum(1 for r in result.runs if r.usage.cost_usd is None)
    cost = _cost_text(usage.cost_usd) if usage.cost_usd is not None else (
        f"${known:.4f} + {unpriced} unpriced run(s)" if result.runs else "n/a"
    )
    console.print(
        f"Total: {usage.input_tokens:,} input ({usage.cached_input_tokens:,} cached), "
        f"{usage.output_tokens:,} output tokens, cost {cost}, {result.duration_s:.1f}s"
    )
    for key in ("scores", "chosen", "best_round", "accepted"):
        if key in result.meta:
            console.print(f"{key}: {result.meta[key]}")
    for failure in result.meta.get("failures", []):
        console.print(f"[yellow]failure:[/yellow] {failure}", highlight=False)
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


def save_record(runs_dir: str, record: dict, patch: str) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(record["team"])).strip("-") or "run"
    out = (Path(runs_dir) / f"{stamp}_{slug}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    record_path, patch_path = out / "record.json", out / "final.patch"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    patch_path.write_bytes(patch.encode("utf-8", "surrogateescape"))
    return record_path, patch_path


async def _apply_to_tree(repo: str, patch_path: Path, console: Console) -> int:
    code, out, _ = await run_process(["git", "status", "--porcelain"], cwd=repo)
    if code != 0 or out.strip():
        console.print("[red]Not applying: the working tree has uncommitted changes.[/red]")
        return EXIT_APPLY
    code, _, err = await run_process(["git", "apply", "--binary", str(patch_path)], cwd=repo)
    if code != 0:
        console.print(f"[red]git apply failed:[/red] {err.decode('utf-8', 'replace').strip()}")
        return EXIT_APPLY
    console.print("Patch applied to your working tree (not committed).")
    return EXIT_OK


async def run_command(args: argparse.Namespace, console: Console) -> int:
    try:
        spec = parse_team_spec(args.team)
        team = build_team(args.team, permissions=args.permissions, timeout=args.timeout)
    except SpecError as exc:
        console.print(f"[red]Invalid --team:[/red] {exc}")
        return EXIT_USAGE
    if args.permissions == "safe" and any(h.harness == "sdk" for h in spec.harness_specs):
        console.print(
            "[yellow]The sdk harness has no sandbox: its shell commands run with your "
            "privileges in the worktree.[/yellow]"
        )
    try:
        workspace = await LocalWorktree.create(args.repo, args.base)
    except (GitError, OSError) as exc:
        console.print(f"[red]Cannot create a worktree:[/red] {exc}")
        return EXIT_USAGE
    repo = workspace.repo
    task = CodingTask(task_id=datetime.now(timezone.utc).strftime("task-%Y%m%dT%H%M%S"), instruction=args.instruction)
    started_at = datetime.now(timezone.utc).isoformat()
    console.print(
        f"Team {team.name} on {repo} @ {workspace.base_commit[:12]} (worktree {workspace.root})",
        soft_wrap=True,
    )

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None

    def on_interrupt() -> None:
        console.print("\n[yellow]Interrupted: cancelling workers and cleaning up...[/yellow]")
        loop.remove_signal_handler(signal.SIGINT)  # a second Ctrl-C force-quits
        main_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_interrupt)
    start = time.perf_counter()
    try:
        result = await team.solve(task, workspace)
    except asyncio.CancelledError:
        return EXIT_CANCELLED
    finally:
        loop.remove_signal_handler(signal.SIGINT)
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
    record_path, patch_path = save_record(args.runs_dir, record, result.patch)
    render_summary(console, team.name, result, repo=repo, patch_path=patch_path, record_path=record_path)
    if not result.patch.strip():
        return EXIT_NO_PATCH
    if args.apply:
        return await _apply_to_tree(repo, patch_path, console)
    return EXIT_OK


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

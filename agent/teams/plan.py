"""The ``plan`` topology: a planner splits the task, workers solve the parts in parallel, an
integrator merges the patches and a check verifies the result.

1. The *planner* (any harness) reads the repository in a fork that is discarded and answers with
   structured JSON subtasks. If it fails or its answer is not a valid plan, that is recorded and the
   team falls back to ``solo`` with the first worker; a plan of one subtask does the same.
2. Each subtask is solved concurrently in its own fork (subtask ``i`` by ``workers[i % len(workers)]``).
3. The patches are merged into the team's own workspace in subtask order with ``git apply --3way``.
   A patch that conflicts or does not apply is rolled back (the clean ones stay), then the
   *integrator* runs in the workspace with the task, the patches and what went wrong, and completes
   the combined change. It also runs when a subtask worker failed, told which subtask is missing.
4. If a check is configured and fails on the integrated patch, the integrator gets one fix pass
   with the check output, then the check runs again.

Every harness run (``planner``, ``worker``, ``integrator``) is in ``TeamResult.runs``, failed ones
included; failures are in ``meta["failures"]``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from agent.workflows import map_concurrently

from .harnesses.check import Check, CheckResult, CheckRunner
from .topologies import (
    MAX_PATCH_IN_PROMPT,
    WORKER_PROMPT,
    _close,
    _failure,
    _record_check,
    _run_harness,
    harness_label,
)
from .types import CodingTask, ExecResult, Harness, HarnessRun, TeamResult, Workspace
from .verdict import json_objects

DEFAULT_MAX_TASKS = 4

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "description": "Independent subtasks that together complete the task.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short name of the subtask."},
                    "instruction": {
                        "type": "string",
                        "description": "Self-contained instructions for the engineer who gets only this subtask.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files or directories the subtask is expected to change.",
                    },
                },
                "required": ["title", "instruction", "paths"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["subtasks"],
    "additionalProperties": False,
}

PLAN_PROMPT = (
    "You are the planner of a team of engineers. Read the repository in the current directory as "
    "needed, but do NOT modify, create or delete any file: you only plan. Split the task below into "
    "between 1 and {max_tasks} subtasks that separate engineers can solve at the same time, each in "
    "its own copy of the repository, without needing each other's results. Prefer subtasks that "
    "touch different files, so their changes merge cleanly. Each subtask's instruction must be "
    "self-contained. If the task cannot be split sensibly, return exactly one subtask. Do not add a "
    "subtask for integration or for running the final tests: that is done afterwards.\n\n"
    "Task:\n{instruction}\n\n"
    "Finish with a single JSON object and nothing after it:\n"
    '{{"subtasks": [{{"title": "<short name>", "instruction": "<self-contained instructions>", '
    '"paths": ["<file or directory it will change>"]}}]}}'
)

SUBTASK_PROMPT = (
    "You are one of several engineers who split a larger task. Each of you solves a different "
    "subtask in a separate copy of the repository and the changes are merged afterwards. Solve ONLY "
    "your subtask by editing the files in the current repository (the current directory). Do not "
    "touch files or behaviour that belong to the other subtasks, and keep your change focused so it "
    "merges cleanly. Run the project's tests where they exist to verify your part. Do not commit.\n\n"
    "Overall task:\n{instruction}\n\n"
    "Your subtask ({number} of {total}): {title}\n{subtask}{paths}\n\n"
    "The other subtasks (not yours; leave them alone):\n{others}"
)

INTEGRATE_PROMPT = (
    "You are the integrator of a team. The task below was split into subtasks that engineers solved "
    "in parallel, each in a separate copy of the repository. Their patches are being merged into "
    "the repository in the current directory. The merged changes are uncommitted modifications of the "
    "working tree (changes to existing files show in `git diff HEAD`, new files are untracked: see "
    "`git status`). Complete the merge so the working tree contains the full, working change that "
    "solves the whole task. Merge conflicting patches by combining both sides' intent, implement any "
    "missing subtask yourself, and run the project's tests. Do not commit.\n\n"
    "Overall task:\n{instruction}\n\n"
    "State of the merge:\n{state}\n\n"
    "Subtask patches:\n{patches}"
)

FIX_PROMPT = (
    "The merged change for the task below is in the working tree of the repository in the current "
    "directory (uncommitted: see `git diff HEAD` and `git status`), but the project's check failed on "
    "it. Fix the change so the check passes, without weakening or deleting the check or its tests. "
    "Do not commit.\n\n"
    "Overall task:\n{instruction}\n\nCheck result:\n{check}"
)


class PlanParseError(ValueError):
    pass


@dataclass(frozen=True)
class Subtask:
    title: str
    instruction: str
    paths: tuple[str, ...] = ()


def parse_plan(text: str, max_tasks: int) -> list[Subtask]:
    """Subtasks from the last JSON object in ``text`` that has a ``subtasks`` list (1..max_tasks)."""
    found: Optional[dict[str, Any]] = None
    for obj in json_objects(text):
        if isinstance(obj.get("subtasks"), list):
            found = obj
    if found is None:
        raise PlanParseError("no JSON object with a 'subtasks' list in the reply")
    items = found["subtasks"]
    if not 1 <= len(items) <= max_tasks:
        raise PlanParseError(f"expected 1 to {max_tasks} subtasks, got {len(items)}")
    subtasks: list[Subtask] = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise PlanParseError(f"subtask {n} is not an object")
        title, instruction, paths = item.get("title"), item.get("instruction"), item.get("paths", [])
        if not isinstance(title, str) or not title.strip():
            raise PlanParseError(f"subtask {n} has no title")
        if not isinstance(instruction, str) or not instruction.strip():
            raise PlanParseError(f"subtask {n} has no instruction")
        if paths is None:
            paths = []
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise PlanParseError(f"subtask {n}: paths must be a list of strings")
        subtasks.append(Subtask(title.strip(), instruction.strip(), tuple(p.strip() for p in paths if p.strip())))
    return subtasks


async def _git(workspace: Workspace, *args: str, stdin: Optional[str] = None) -> ExecResult:
    return await workspace.exec(["git", *args], stdin=stdin)


async def _apply_3way(workspace: Workspace, patch: str) -> tuple[Optional[str], bool]:
    """Merge ``patch`` into the workspace's current state: ``(None, True)`` on success.

    On failure: ``(error, restored)``. The workspace is rolled back to exactly its state before the
    attempt (a conflicting ``git apply --3way`` would otherwise leave conflict markers and an
    unmerged index behind); ``restored`` is False if even that failed, in which case the caller must
    not stack further patches on it. Git's exit codes are checked at every step: ``Workspace.exec``
    returns failures, it does not raise them.
    """
    snapshot: Optional[str] = None
    try:
        res = await _git(workspace, "add", "--all")
        if res.exit_code != 0:
            return f"could not stage the workspace: {_say(res)}", True
        res = await _git(workspace, "write-tree")
        if res.exit_code != 0:
            return f"could not snapshot the workspace: {_say(res)}", True
        snapshot = res.stdout.strip()
        res = await _git(workspace, "apply", "--3way", "--binary", "--whitespace=nowarn", "-", stdin=patch)
        if res.exit_code == 0:
            return None, True
        error = _say(res)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if snapshot is None:  # nothing was changed yet
        return error, True
    try:
        for argv in (("read-tree", "--reset", "-u", snapshot), ("clean", "-fdq")):
            res = await _git(workspace, *argv)
            if res.exit_code != 0:
                return f"{error} (and the rollback failed: git {argv[0]}: {_say(res)})", False
    except Exception as exc:
        return f"{error} (and the rollback failed: {type(exc).__name__}: {exc})", False
    return error, True


def _say(res: ExecResult) -> str:
    return ((res.stderr or res.stdout).strip() or f"git exited with {res.exit_code}")[:1000]


@dataclass
class _SubtaskState:
    index: int  # 0-based
    subtask: Subtask
    harness: Harness
    patch: str = ""
    runs: list[HarnessRun] = field(default_factory=list)
    error: Optional[str] = None
    apply_error: Optional[str] = None
    status: str = "pending"  # applied | conflict | failed | empty


def _shown(patch: str, limit: int) -> str:
    if len(patch) <= limit:
        return patch
    return patch[:limit] + "\n... [patch truncated; ask git or the files for the rest]"


@dataclass
class PlanTeam:
    """Planner → parallel workers in forks → integrator in the team's workspace → optional check.

    See the module docstring. ``max_tasks`` bounds the plan; a plan with more subtasks is invalid.
    ``integrator`` defaults to the first worker's harness.
    """

    planner: Harness
    workers: Sequence[Harness]
    integrator: Optional[Harness] = None
    max_tasks: int = DEFAULT_MAX_TASKS
    name: str = ""
    timeout: Optional[float] = None
    check: Optional[Check] = None
    max_concurrency: Optional[int] = None

    def __post_init__(self) -> None:
        if len(self.workers) < 1:
            raise ValueError("PlanTeam needs at least one worker")
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.max_tasks < 1:
            raise ValueError("max_tasks must be >= 1")
        self.integrator = self.integrator or self.workers[0]
        self.name = self.name or (
            f"plan:planner={harness_label(self.planner)},"
            f"workers={'+'.join(harness_label(w) for w in self.workers)},"
            f"integrator={harness_label(self.integrator)},max_tasks={self.max_tasks}"
        )

    async def _plan(
        self, task: CodingTask, workspace: Workspace
    ) -> tuple[Optional[HarnessRun], list[Subtask], Optional[str]]:
        """Run the planner in a throwaway fork; the run (if any), the subtasks, the error (if any)."""
        prompt = PLAN_PROMPT.format(max_tasks=self.max_tasks, instruction=task.instruction)
        kwargs: dict[str, Any] = {}
        if getattr(self.planner, "supports_output_schema", False):
            kwargs["output_schema"] = PLAN_SCHEMA
        fork: Optional[Workspace] = None
        run: Optional[HarnessRun] = None
        try:
            fork = await workspace.fork()
            run = await self.planner.run(fork, prompt, role="planner", timeout=self.timeout, **kwargs)
            if run.exit_code != 0:
                raise RuntimeError(f"exit code {run.exit_code}: {run.final_message[:300]}")
            return run, parse_plan(run.final_message, self.max_tasks), None
        except Exception as exc:
            return run, [], f"{type(exc).__name__}: {exc}"
        finally:
            if fork is not None:
                await _close(fork)

    async def _solo(
        self, task: CodingTask, workspace: Workspace, meta: dict[str, Any], runs: list[HarnessRun], reason: str
    ) -> str:
        meta["fallback"] = {"to": "solo", "worker": harness_label(self.workers[0]), "reason": reason}
        run, error = await _run_harness(
            self.workers[0],
            workspace,
            WORKER_PROMPT.format(instruction=task.instruction),
            role="worker",
            timeout=self.timeout,
        )
        if run is not None:
            runs.append(run)
        if error:
            meta.setdefault("failures", []).append(_failure(role="worker", error=error))
        return await workspace.diff()

    async def solve(self, task: CodingTask, workspace: Workspace) -> TeamResult:
        start = time.perf_counter()
        meta: dict[str, Any] = {"team": self.name, "planner": harness_label(self.planner)}
        runs: list[HarnessRun] = []
        checker = self.check.bind(workspace) if self.check else None

        plan_run, subtasks, plan_error = await self._plan(task, workspace)
        if plan_run is not None:
            runs.append(plan_run)
        if plan_error:
            meta["failures"] = [_failure(role="planner", error=plan_error)]
            patch = await self._solo(task, workspace, meta, runs, "the planner did not return a valid plan")
        elif len(subtasks) == 1:
            meta["subtasks"] = [self._entry(None, subtasks[0])]
            patch = await self._solo(task, workspace, meta, runs, "the plan has a single subtask")
        else:
            patch = await self._run_plan(task, workspace, subtasks, meta, runs, checker)
        if checker is not None and "check" not in meta:
            await _record_check(meta, checker, patch)
        return TeamResult(patch, runs, time.perf_counter() - start, meta)

    @staticmethod
    def _entry(state: Optional[_SubtaskState], subtask: Subtask) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "title": subtask.title,
            "instruction": subtask.instruction,
            "paths": list(subtask.paths),
        }
        if state is not None:
            entry.update(worker=harness_label(state.harness), status=state.status)
        return entry

    async def _run_plan(
        self,
        task: CodingTask,
        workspace: Workspace,
        subtasks: list[Subtask],
        meta: dict[str, Any],
        runs: list[HarnessRun],
        checker: Optional[CheckRunner],
    ) -> str:
        assert self.integrator is not None
        total = len(subtasks)
        states = [
            _SubtaskState(i, s, self.workers[i % len(self.workers)]) for i, s in enumerate(subtasks)
        ]

        async def work(state: _SubtaskState) -> _SubtaskState:
            others = "\n".join(
                f"{o.index + 1}. {o.subtask.title}" for o in states if o.index != state.index
            )
            paths = f"\nFiles likely involved: {', '.join(state.subtask.paths)}" if state.subtask.paths else ""
            prompt = SUBTASK_PROMPT.format(
                instruction=task.instruction,
                number=state.index + 1,
                total=total,
                title=state.subtask.title,
                subtask=state.subtask.instruction,
                paths=paths,
                others=others,
            )
            fork: Optional[Workspace] = None
            try:
                fork = await workspace.fork()
                run, error = await _run_harness(state.harness, fork, prompt, role="worker", timeout=self.timeout)
                if run is not None:
                    state.runs.append(run)
                state.error = error
                state.patch = await fork.diff()
            except Exception as exc:
                note = f"{type(exc).__name__}: {exc}"
                state.error = f"{state.error}; {note}" if state.error else note
            finally:
                if fork is not None:
                    await _close(fork)
            return state

        await map_concurrently(states, work, max_concurrency=min(total, self.max_concurrency or total))

        failures: list[dict[str, Any]] = list(meta.get("failures", []))
        first_worker_run = len(runs)
        for state in states:
            runs.extend(state.runs)
            if not state.patch.strip():
                state.status = "failed" if state.error else "empty"
                failures.append(
                    _failure(
                        subtask=state.index + 1,
                        role="worker",
                        error=state.error or "the worker produced no changes",
                    )
                )
            elif state.error:  # the worker errored after changing files: keep the change, note it
                failures.append(_failure(subtask=state.index + 1, role="worker", error=state.error))

        clean = True
        for state in states:
            if state.status in ("failed", "empty"):
                continue
            if not clean:
                state.apply_error = "not attempted: the workspace could not be restored after an earlier failed merge"
            else:
                state.apply_error, clean = await _apply_3way(workspace, state.patch)
            state.status = "conflict" if state.apply_error else "applied"
        # `git apply --3way` stages what it touches; unstage so the workspace looks like every
        # other topology's (changes in `git diff HEAD`, new files untracked).
        res = await _git(workspace, "reset", "-q")
        if res.exit_code != 0:
            failures.append(_failure(role="integration", error=f"git reset failed: {res.stderr.strip()[:300]}"))

        problems = [s for s in states if s.status != "applied"]
        integration: dict[str, Any] = {
            "method": "3way",
            "applied": [s.index + 1 for s in states if s.status == "applied"],
            "conflicts": [s.index + 1 for s in states if s.status == "conflict"],
            "missing": [s.index + 1 for s in states if s.status in ("failed", "empty")],
            "integrator_runs": 0,
        }
        meta["integration"] = integration

        async def run_integrator(prompt: str) -> None:
            assert self.integrator is not None
            run, error = await _run_harness(
                self.integrator, workspace, prompt, role="integrator", timeout=self.timeout
            )
            if run is not None:
                runs.append(run)
            integration["integrator_runs"] += 1
            if error:
                failures.append(_failure(role="integrator", error=error))

        if problems:
            await run_integrator(self._integrate_prompt(task, states))

        patch = await workspace.diff()
        if checker is not None:
            result: CheckResult = await checker(patch)
            if not result.passed and patch.strip():
                meta["check_before_fix"] = result.to_dict()
                await run_integrator(FIX_PROMPT.format(instruction=task.instruction, check=result.describe()))
                patch = await workspace.diff()
                result = await checker(patch)
            meta["check"] = result.to_dict()

        offset = first_worker_run
        entries = []
        for state in states:
            entry = self._entry(state, state.subtask)
            entry["runs"] = list(range(offset, offset + len(state.runs)))
            offset += len(state.runs)
            entries.append(entry)
        meta["subtasks"] = entries
        if failures:
            meta["failures"] = failures
        return patch

    @staticmethod
    def _integrate_prompt(task: CodingTask, states: list[_SubtaskState]) -> str:
        lines = []
        for s in states:
            n, title = s.index + 1, s.subtask.title
            if s.status == "applied":
                lines.append(f"- Subtask {n} ({title}): its patch was merged cleanly and is in the working tree.")
            elif s.status == "conflict":
                lines.append(
                    f"- Subtask {n} ({title}): its patch could NOT be merged ({s.apply_error}). It is "
                    "not in the working tree; re-create its intent on top of the current state."
                )
            else:
                reason = s.error or "its worker produced no changes"
                lines.append(
                    f"- Subtask {n} ({title}): MISSING ({reason}). Complete it yourself: "
                    f"{s.subtask.instruction}"
                )
        with_patch = [s for s in states if s.patch.strip()]
        budget = max(1, MAX_PATCH_IN_PROMPT // max(len(with_patch), 1))  # all patches together stay bounded
        patches = "\n\n".join(
            f"### Subtask {s.index + 1}: {s.subtask.title} "
            f"({'already in the working tree' if s.status == 'applied' else 'NOT applied'})\n"
            f"```diff\n{_shown(s.patch, budget)}\n```"
            for s in with_patch
        ) or "(no subtask produced a patch)"
        return INTEGRATE_PROMPT.format(
            instruction=task.instruction, state="\n".join(lines), patches=patches
        )

"""Grade the patches a team produced but did not submit: best-of-N candidates and review rounds.

``run`` stores them in each record (``meta.candidates[].patch``, ``meta.round_log[].patch``) and
grades only the submitted patch, so a run costs the same either way. ``grade_candidates`` grades
the others afterwards, from the records, with the submitted patch's grader and hidden tests
(``evaluate.py``), each in a fresh grading sandbox; no agent runs and no model is called. It
appends one line per distinct patch of a pair to ``candidates.jsonl`` beside ``results.jsonl``,
which it never changes; ``report`` reads both and adds oracle pass@k and per-round reviewer
precision.

Not graded: a patch identical to the submitted one (it has the record's verdict), an empty patch
(unresolved, as upstream), a patch the credential scrub changed (the stored copy is not what the
agent wrote), plan subtasks (each is only part of a solution) and errored pairs. Resumable: patches
already graded are skipped, grading errors are retried on the next invocation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.workflows.concurrency import map_concurrently

from .dataset import BenchTask
from .evaluate import EvalResult
from .redact import Redactor
from .run import ARTIFACTS_DIR, RESULTS_FILE, Evaluator, extra_patches, load_records, slug

CANDIDATES_FILE = "candidates.jsonl"
GRADED_LABELS = ("candidate-", "round-")


def patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest()


def ungraded_patches(record: dict[str, Any]) -> dict[str, list[str]]:
    """``{patch: labels}``: the record's stored candidate and round patches that need grading."""
    if record.get("error") is not None or record.get("stage") != "done":
        return {}
    found: dict[str, list[str]] = {}
    for label, entry in extra_patches(record.get("meta")):
        patch = entry["patch"]
        if (
            label.startswith(GRADED_LABELS)
            and patch.strip()
            and patch != record.get("patch")
            and not entry.get("patch_redacted")
        ):
            found.setdefault(patch, []).append(label)
    return found


def load_grades(out_dir: str | Path) -> dict[tuple[str, str, str], bool]:
    """``(task_id, team, patch sha256) -> resolved`` from ``candidates.jsonl``; the latest
    successful grade of a patch wins, failed gradings are left out."""
    grades = {}
    for line in load_records(Path(out_dir) / CANDIDATES_FILE):
        if line.get("error") is None:
            grades[(line["task_id"], line["team"], line["patch_sha256"])] = bool(line["resolved"])
    return grades


def compact_eval(evaluation: EvalResult) -> dict[str, Any]:
    """``EvalResult.to_json`` with the passing PASS_TO_PASS tests counted instead of listed (they
    run to thousands of names and make most of a record's size)."""
    data = evaluation.to_json()
    p2p = (data.get("report") or {}).get("PASS_TO_PASS")
    if p2p is not None:
        p2p["success_count"] = len(p2p.pop("success"))
    return data


@dataclass
class GradeSummary:
    graded: int = 0
    resolved: int = 0
    errored: int = 0
    already_graded: int = 0
    no_task: list[str] = field(default_factory=list)  # task ids missing from the dataset


async def grade_candidates(
    out_dir: str | Path,
    tasks: Sequence[BenchTask],
    *,
    evaluator: Evaluator,
    concurrency: int = 4,
    teams: Optional[Sequence[str]] = None,
    log: Callable[[str], None] = print,
    redactor: Optional[Redactor] = None,
) -> GradeSummary:
    """Grade the ungraded candidate and round patches of the latest record of every pair (of
    ``teams``, default all) in ``out_dir``; ``tasks`` supply the hidden tests."""
    out = Path(out_dir)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in load_records(out / RESULTS_FILE):
        if teams is None or record["team"] in teams:
            latest[(record["task_id"], record["team"])] = record
    path = out / CANDIDATES_FILE
    done = {
        (r["task_id"], r["team"], r["patch_sha256"])
        for r in load_records(path, repair_tail=True)
        if r.get("error") is None
    }
    by_id = {t.task_id: t for t in tasks}
    summary = GradeSummary()
    jobs = []
    for (task_id, team), record in sorted(latest.items()):
        for patch, labels in ungraded_patches(record).items():
            if (task_id, team, patch_sha256(patch)) in done:
                summary.already_graded += 1
            elif task_id not in by_id:
                summary.no_task.append(task_id)
            else:
                jobs.append((by_id[task_id], team, patch, labels))
    summary.no_task = sorted(set(summary.no_task))
    redactor = redactor or Redactor()
    lock = asyncio.Lock()

    async def grade(job: tuple[BenchTask, str, str, list[str]]) -> None:
        task, team, patch, labels = job
        line: dict[str, Any] = {
            "task_id": task.task_id,
            "team": team,
            "patch_sha256": patch_sha256(patch),
            "labels": labels,
            "resolved": None,
            "eval": None,
            "error": None,
        }
        try:
            evaluation = await evaluator(task, patch)
            line["resolved"] = evaluation.resolved
            line["eval"] = compact_eval(evaluation)
            if evaluation.log:
                directory = out / ARTIFACTS_DIR / slug(team) / slug(task.task_id)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{labels[0]}.eval.log").write_text(
                    redactor.text(evaluation.log), encoding="utf-8", errors="replace"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            line["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        line = redactor.obj(line)
        async with lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=True) + "\n")
            if line["error"]:
                summary.errored += 1
            else:
                summary.graded += 1
                summary.resolved += bool(line["resolved"])
        verdict = (
            f"ERROR, retried on the next run: {line['error'].splitlines()[0]}"
            if line["error"]
            else ("resolved" if line["resolved"] else "unresolved")
        )
        log(f"{task.task_id} x {team} {','.join(labels)}: {verdict}")

    await map_concurrently(jobs, grade, max_concurrency=concurrency)
    return summary

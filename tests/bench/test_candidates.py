import asyncio
import json

import pytest

from agent.bench.candidates import CANDIDATES_FILE, grade_candidates, load_grades, patch_sha256
from agent.bench.cli import main
from agent.bench.dataset import BenchTask, GradingSpec
from agent.bench.evaluate import EvalResult, GradeReport
from agent.bench.redact import Redactor
from agent.bench.report import build_report, render, summarize
from agent.bench.run import load_records

from .test_report import BEST, PASSING, REVIEW, graded, table

PLAN = "plan:planner=claude,workers=claude"


def task(task_id):
    return BenchTask(
        task_id=task_id, instruction="issue", repo="o/r", base_commit="a" * 40, image="img",
        created_at=None, language="go", dataset="test",
        grading=GradingSpec(test_patch="", fail_to_pass=("f",), pass_to_pass=("p1", "p2"), gold_patch="g"),
    )


def done(task_id, team, patch, resolved, meta, **extra):
    return {
        "task_id": task_id, "team": team, "patch": patch, "resolved": resolved, "meta": meta,
        "error": None, "stage": "done", **extra,
    }


def write_records(directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


class Grader:
    """Resolves patches containing "good"; the first grading of "boom" fails."""

    def __init__(self):
        self.calls = []

    async def __call__(self, task, patch):
        self.calls.append((task.task_id, patch))
        if patch == "boom" and self.calls.count((task.task_id, patch)) == 1:
            raise RuntimeError("sandbox exploded")
        ok = "good" in patch
        report = GradeReport(resolved=ok, fail_to_pass_success=["f"] if ok else [], pass_to_pass_success=["p1", "p2"])
        return EvalResult(resolved=ok, report=report, log=f"log of {patch}")


def stored_run():
    best_meta = {"candidates": [{"patch": "A good"}, {"patch": "B"}, {"patch": "B"}, {"patch": ""}]}
    return [
        done("t1", BEST, "A good", True, best_meta),
        done("t1", REVIEW, "R2", False, {"round_log": [{"patch": "R1 good"}, {"patch": "R2"}]}),
        done("t2", BEST, "D", False, {"candidates": [
            {"patch": "C [REDACTED]", "patch_redacted": True}, {"patch": "D"}, {"patch": "boom"},
        ]}),
        {**done("t3", BEST, "E", None, {"candidates": [{"patch": "F"}]}), "error": "evaluate: x", "stage": "evaluate"},
        done("t1", PLAN, "S1S2", True, {"subtasks": [{"patch": "S1"}, {"patch": "S2"}]}),
        done("t4", BEST, "G", False, {"candidates": [{"patch": "H"}]}),
    ]


def test_only_candidate_and_round_patches_that_need_a_verdict_are_graded_once_each(tmp_path):
    write_records(tmp_path, stored_run())
    grader = Grader()
    tasks = [task("t1"), task("t2"), task("t3")]  # t4 is not in the dataset

    def grade():
        return asyncio.run(
            grade_candidates(tmp_path, tasks, evaluator=grader, log=lambda m: None, redactor=Redactor(host_env=False))
        )

    summary = grade()
    # Not graded: the submitted patch, an empty or scrubbed patch, plan subtasks, errored pairs;
    # a patch two candidates share is graded once.
    assert sorted(grader.calls) == [("t1", "B"), ("t1", "R1 good"), ("t2", "boom")]
    assert (summary.graded, summary.resolved, summary.errored, summary.no_task) == (2, 1, 1, ["t4"])
    lines = {(r["task_id"], r["team"], tuple(r["labels"])): r for r in load_records(tmp_path / CANDIDATES_FILE)}
    b = lines[("t1", BEST, ("candidate-2", "candidate-3"))]
    assert b["resolved"] is False and b["patch_sha256"] == patch_sha256("B")
    assert b["eval"]["report"]["PASS_TO_PASS"] == {"success_count": 2, "failure": [], "missing": []}
    assert lines[("t1", REVIEW, ("round-1",))]["resolved"] is True
    assert "sandbox exploded" in lines[("t2", BEST, ("candidate-3",))]["error"]
    log = tmp_path / "artifacts" / "best_workers=claude+claude+claude,judge=claude" / "t1" / "candidate-2.eval.log"
    assert log.read_text() == "log of B"

    # Resuming grades only what failed.
    again = grade()
    assert grader.calls[3:] == [("t2", "boom")]
    assert (again.graded, again.errored, again.already_graded) == (1, 0, 2)
    assert load_grades(tmp_path) == {
        ("t1", BEST, patch_sha256("B")): False,
        ("t1", REVIEW, patch_sha256("R1 good")): True,
        ("t2", BEST, patch_sha256("boom")): False,
    }


def graded_with(task_id, team, patch, resolved, meta):
    record = graded(task_id, team, resolved, 0.1, 60, meta=meta, report=PASSING)
    record["patch"] = patch
    return record


def rounds(*entries):
    """A review team's meta from ``(patch, passed, changed[, reviewed])`` per round."""
    log = []
    for n, (patch, passed, changed, *reviewed) in enumerate(entries, 1):
        log.append({"round": n, "patch": patch, "passed": passed, "changed": changed,
                    "reviewed": reviewed[0] if reviewed else True})
    return {"round_log": log}


def candidate_records():
    return [
        graded_with("t1", BEST, "A", True, {"candidates": [{"patch": "A"}, {"patch": "B"}, {"patch": "C"}]}),
        graded_with("t2", BEST, "D", False, {"candidates": [{"patch": "D"}, {"patch": "E"}, {"patch": ""}]}),
        graded_with("t3", BEST, "G", False, {"candidates": [{"patch": "G"}, {"patch": "H"}]}),  # H ungraded
        graded_with("t1", REVIEW, "R2", True, rounds(("R1", False, True), ("R2", True, True))),
        graded_with("t2", REVIEW, "S2", False, rounds(("S1", False, True), ("S2", True, True))),
        graded_with("t3", REVIEW, "U", False, rounds(("U", False, True), ("U", False, False), ("", False, True, False))),
    ]


GRADES = {
    ("t1", BEST, patch_sha256("B")): False,
    ("t1", BEST, patch_sha256("C")): True,
    ("t2", BEST, patch_sha256("E")): True,
    ("t1", REVIEW, patch_sha256("R1")): True,
    ("t2", REVIEW, patch_sha256("S1")): False,
}


def test_report_shows_oracle_pass_at_k_and_per_round_reviewer_precision_once_graded():
    text = render(summarize(candidate_records(), grades=GRADES))
    best = table(text, "| Team | Tasks | Candidates")[BEST]
    assert best["Tasks"] == "2 of 3" and best["Candidates"] == "3"  # t3 lacks a verdict for H
    assert best["Judge's pick resolved"] == "1/2 (50%)" and best["Oracle pass@k"] == "2/2 (100%)"
    assert best["Pick missed a resolving candidate"] == "1"
    review = table(text, "| Team | Rounds")[REVIEW]
    cells = ("Approved, resolved", "Approved, unresolved", "Rejected, resolved", "Rejected, unresolved")
    # Left out: t3's unchanged round 2 and its empty round 3, which no reviewer judged.
    assert review["Rounds"] == "5" and [review[c] for c in cells] == ["1", "1", "1", "2"]
    assert review["Precision"] == "1/2 (50%)" and review["Specificity"] == "2/3 (67%)"

    # Nothing new before grading, or for records without stored patches (the pilot's).
    assert "## Candidates and review rounds" not in render(summarize(candidate_records()))
    bare = [{**r, "meta": {k: [{"score": 1} for _ in v] for k, v in r["meta"].items()}} for r in candidate_records()]
    assert "## Candidates and review rounds" not in render(summarize(bare, grades=GRADES))


def test_report_command_reads_the_grades_beside_the_results(tmp_path, capsys):
    write_records(tmp_path, candidate_records())
    assert "Candidates and review rounds" not in build_report(tmp_path)
    (tmp_path / CANDIDATES_FILE).write_text(
        "".join(
            json.dumps({"task_id": t, "team": team, "patch_sha256": sha, "resolved": ok, "error": None}) + "\n"
            for (t, team, sha), ok in GRADES.items()
        )
        + json.dumps({"task_id": "t3", "team": BEST, "patch_sha256": patch_sha256("H"), "resolved": None,
                      "error": "boom"}) + "\n"
    )
    assert main(["report", str(tmp_path)]) == 0
    assert table(capsys.readouterr().out, "| Team | Tasks | Candidates")[BEST]["Tasks"] == "2 of 3"


def test_grade_candidates_command_grades_in_the_runs_recorded_environment(tmp_path, monkeypatch, capsys):
    import agent.bench.cli as cli

    write_records(tmp_path, stored_run()[:2])
    run = {"dataset": "multilang-test", "backend": "docker", "add_python": None, "registry_secret": None}
    (tmp_path / "meta.json").write_text(json.dumps({"runs": [run]}))
    loaded, backends = [], []

    def load_tasks(dataset, instance_ids):
        loaded.append((dataset, instance_ids))
        return [task(i) for i in instance_ids]

    def backend(args):
        backends.append((args.backend, args.add_python, args.registry_secret))
        return "backend"

    grader = Grader()
    monkeypatch.setattr(cli, "load_tasks", load_tasks)
    monkeypatch.setattr(cli, "_backend", backend)
    monkeypatch.setattr(cli, "evaluate_patch", lambda task, patch, backend: grader(task, patch))
    assert main(["grade-candidates", str(tmp_path), "--team", BEST]) == 0
    assert loaded == [("multilang-test", ["t1"])] and grader.calls == [("t1", "B")]
    assert backends == [("docker", None, None)]  # the run's backend, not the CLI default
    assert "graded 1 patch(es), 0 resolved" in capsys.readouterr().out
    assert len(load_records(tmp_path / CANDIDATES_FILE)) == 1

    # A flag that contradicts the run is refused before anything is graded.
    for flag in (["--backend", "modal"], ["--add-python", "3.12"], ["--registry-secret", "hub"]):
        assert main(["grade-candidates", str(tmp_path), *flag]) == 2
        assert "contradicts the run's" in capsys.readouterr().err
    assert main(["grade-candidates", str(tmp_path), "--backend", "docker"]) == 0  # agreeing is fine
    # A run recorded before these settings were stored (the pilot) takes them from the flags.
    (tmp_path / "meta.json").write_text(json.dumps({"runs": [{"backend": "modal"}]}))
    assert main(["grade-candidates", str(tmp_path), "--add-python", "3.12"]) == 0
    assert backends[-1] == ("modal", "3.12", None)
    assert len(backends) == 3


def test_run_records_the_options_that_shape_the_grading_image(tmp_path, monkeypatch):
    import agent.bench.cli as cli

    seen = {}

    async def run_benchmark(tasks, teams, out, **kwargs):
        seen.update(kwargs["run_meta"])
        from agent.bench.run import RunSummary

        return RunSummary()

    monkeypatch.setattr(cli, "_select", lambda args: [task("t1")])
    monkeypatch.setattr(cli, "_backend", lambda args, teams=(): "backend")
    monkeypatch.setattr(cli, "run_benchmark", run_benchmark)
    argv = ["run", "--team", "solo:worker=claude", "--out", str(tmp_path), "--add-python", "3.12",
            "--registry-secret", "hub-creds"]
    assert main(argv) == 0
    assert (seen["backend"], seen["add_python"], seen["registry_secret"]) == ("modal", "3.12", "hub-creds")


@pytest.mark.parametrize("directory, code", [("runs/x", 0), ("/abs/runs/x", 2)])
def test_remote_runs_grade_candidates_on_a_volume_directory(monkeypatch, directory, code):
    import sys
    import types

    launched = []
    stub = types.ModuleType("agent.bench.remote")
    stub.VOLUME = "v"
    stub.launch = lambda argv: launched.append(argv) or ("app", "call")
    monkeypatch.setitem(sys.modules, "agent.bench.remote", stub)
    assert main(["remote", "grade-candidates", directory]) == code
    assert launched == ([["grade-candidates", directory]] if code == 0 else [])

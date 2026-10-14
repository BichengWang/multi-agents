import asyncio
import json
from datetime import datetime, timezone

import pytest

from agent.bench.cli import main
from agent.bench.dataset import BenchTask, GradingSpec
from agent.bench.evaluate import EvalResult
from agent.bench.run import (
    BudgetError,
    load_records,
    required_secret_keys,
    run_benchmark,
    validate_gold,
)
from agent.teams.types import CodingTask

from .fakes import FakeTeam, MemoryWorkspace

SECRET_TEST_NAME = "tests/hidden_test.py::test_secret"


def make_tasks(n):
    return [
        BenchTask(
            task_id=f"o__r-{i}",
            instruction=f"issue {i}",
            repo="o/r",
            base_commit="a" * 40,
            image=f"img:{i}",
            created_at=datetime(2026, 5, i + 1, tzinfo=timezone.utc),
            language="go",
            dataset="test",
            grading=GradingSpec(
                test_patch="diff --git a/x b/x\n",
                fail_to_pass=(SECRET_TEST_NAME,),
                pass_to_pass=(),
                gold_patch=f"gold-{i}",
            ),
        )
        for i in range(n)
    ]


class Harness:
    """Fake team factory / workspace factory / evaluator wired to the run orchestration."""

    def __init__(self, *teams, resolve=lambda task_id, patch: True, eval_fail=None):
        self.teams = {t.name: t for t in teams}
        self.workspaces = []
        self.graded = []
        self.eval_fail = eval_fail or set()
        self.resolve = resolve
        self.eval_calls = 0
        MemoryWorkspace.created = self.workspaces

    def team_factory(self, spec):
        return self.teams[spec]

    async def workspace_factory(self, task):
        return MemoryWorkspace()

    async def evaluator(self, task, patch):
        self.eval_calls += 1
        if task.task_id in self.eval_fail:
            raise RuntimeError("sandbox exploded")
        self.graded.append((task.task_id, patch))
        return EvalResult(resolved=self.resolve(task.task_id, patch), log="test log")

    def run(self, out, tasks, specs=None, **kwargs):
        kwargs.setdefault("unpriced_fn", lambda spec: [])
        kwargs.setdefault("log", lambda message: None)
        return asyncio.run(
            run_benchmark(
                tasks,
                specs or list(self.teams),
                out,
                team_factory=self.team_factory,
                workspace_factory=self.workspace_factory,
                evaluator=self.evaluator,
                **kwargs,
            )
        )


def test_every_pair_gets_a_fresh_workspace_a_team_run_and_a_graded_record(tmp_path):
    solo, review = FakeTeam("solo:worker=claude"), FakeTeam("review:coder=claude,reviewer=claude", cost=1.0)
    h = Harness(solo, review, resolve=lambda task_id, patch: task_id != "o__r-1")
    summary = h.run(tmp_path, make_tasks(3))

    assert (summary.attempted, summary.errored, summary.resolved) == (6, 0, 4)
    assert len(h.workspaces) == 6 and all(w.closed for w in h.workspaces)
    assert len({id(w) for w in h.workspaces}) == 6
    records = load_records(tmp_path / "results.jsonl")
    assert len(records) == 6
    rec = next(r for r in records if r["task_id"] == "o__r-0" and r["team"].startswith("review"))
    assert rec["resolved"] is True and rec["error"] is None and rec["stage"] == "done"
    assert rec["patch"].startswith("diff --git a/o__r-0") and rec["language"] == "go"
    assert rec["usage"] == {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "cost_usd": 1.0}
    assert rec["duration_s"] == 2.0 and rec["created_at"].startswith("2026-05-01")
    assert rec["runs"][0]["harness"] == "claude" and rec["runs"][0]["usage"]["cost_usd"] == 1.0
    assert rec["eval"]["resolved"] is True
    assert summary.spent_usd == pytest.approx(3 * 0.5 + 3 * 1.0)
    artifacts = tmp_path / "artifacts" / "review_coder=claude,reviewer=claude" / "o__r-0"
    assert (artifacts / "patch.diff").read_text().startswith("diff --git")
    assert json.loads((artifacts / "runs.json").read_text())["runs"][0]["log"] == "LOG"
    assert (artifacts / "eval.log").read_text() == "test log"
    assert json.loads((tmp_path / "meta.json").read_text())["teams"] == [solo.name, review.name]


def test_agents_receive_only_the_visible_task_never_hidden_tests(tmp_path):
    team = FakeTeam("solo:worker=claude")
    Harness(team).run(tmp_path, make_tasks(2))
    for seen in team.tasks_seen:
        assert type(seen) is CodingTask
        assert SECRET_TEST_NAME not in repr(seen) and "gold-" not in repr(seen)
        assert not hasattr(seen, "grading")


def test_resume_skips_finished_pairs_and_retries_only_errors(tmp_path):
    flaky = FakeTeam("solo:worker=claude", fail_for={"o__r-1"})
    h = Harness(flaky)
    first = h.run(tmp_path, make_tasks(3))
    assert (first.attempted, first.errored) == (3, 1)
    records = load_records(tmp_path / "results.jsonl")
    errored = next(r for r in records if r["error"])
    assert errored["task_id"] == "o__r-1" and errored["error"].startswith("solve: RuntimeError")
    assert errored["resolved"] is None and errored["stage"] == "solve"

    flaky.fail_for = set()
    flaky.solved.clear()
    second = h.run(tmp_path, make_tasks(3))
    assert (second.attempted, second.skipped_finished, second.errored) == (1, 2, 0)
    assert flaky.solved == ["o__r-1"]

    third = h.run(tmp_path, make_tasks(3))
    assert (third.attempted, third.skipped_finished) == (0, 3)
    assert len(load_records(tmp_path / "results.jsonl")) == 4  # append-only; retry superseded the error

    opted_out = Harness(FakeTeam("solo:worker=claude", fail_for={"o__r-1"}))
    out2 = tmp_path / "second"
    opted_out.run(out2, make_tasks(3))
    again = opted_out.run(out2, make_tasks(3), retry_errors=False)
    assert again.attempted == 0


def test_grading_errors_regrade_the_saved_patch_without_paying_for_the_agents_again(tmp_path):
    team = FakeTeam("solo:worker=claude", cost=2.0)
    h = Harness(team, eval_fail={"o__r-0"})
    first = h.run(tmp_path, make_tasks(1))
    assert first.errored == 1 and first.spent_usd == 2.0  # the agent cost is kept on the record
    record = load_records(tmp_path / "results.jsonl")[0]
    assert record["stage"] == "evaluate" and record["error"].startswith("evaluate: RuntimeError")
    assert record["patch"]

    h.eval_fail.clear()
    team.solved.clear()
    second = h.run(tmp_path, make_tasks(1))
    assert team.solved == []  # not re-solved
    final = load_records(tmp_path / "results.jsonl")[-1]
    assert final["error"] is None and final["resolved"] is True
    assert final["usage"]["cost_usd"] == 2.0 and final["resumed_from_error"].startswith("evaluate:")
    assert second.spent_usd == pytest.approx(2.0)  # counted once, not twice


def test_budget_guard_stops_launching_pairs_once_priced_spend_reaches_the_cap(tmp_path):
    team = FakeTeam("solo:worker=claude", cost=1.0)
    h = Harness(team)
    summary = h.run(tmp_path, make_tasks(6), concurrency=1, max_cost_usd=2.5)
    assert summary.attempted == 3  # 0, 1, 2 -> 3.0 >= 2.5; nothing more starts
    assert summary.skipped_budget == 3 and summary.spent_usd == 3.0
    assert summary.halted == "budget reached"
    assert team.solved == ["o__r-0", "o__r-1", "o__r-2"]

    # Spend from earlier invocations counts: the resumed run launches nothing new.
    resumed = h.run(tmp_path, make_tasks(6), concurrency=1, max_cost_usd=2.5)
    assert resumed.attempted == 0 and resumed.skipped_budget == 3
    # A larger budget continues from where it stopped.
    more = h.run(tmp_path, make_tasks(6), concurrency=1, max_cost_usd=10)
    assert more.attempted == 3 and more.spent_usd == 6.0


def test_no_budget_means_no_limit_even_for_unpriced_costs(tmp_path):
    team = FakeTeam("solo:worker=codex", cost=None)
    summary = Harness(team).run(tmp_path, make_tasks(3), unpriced_fn=lambda spec: ["codex"])
    assert summary.attempted == 3 and summary.unpriced_pairs == 3


def test_budgeted_run_refuses_unpriced_harnesses_before_starting_anything(tmp_path):
    team = FakeTeam("solo:worker=codex", cost=None)
    h = Harness(team)
    with pytest.raises(BudgetError, match="--allow-unpriced"):
        h.run(tmp_path, make_tasks(2), max_cost_usd=5, unpriced_fn=lambda spec: ["codex"])
    assert h.workspaces == [] and not (tmp_path / "results.jsonl").exists()

    # With the override it runs, but stops launching as soon as an unpriced pair shows up.
    summary = h.run(
        tmp_path, make_tasks(4), concurrency=1, max_cost_usd=5, allow_unpriced=True,
        unpriced_fn=lambda spec: ["codex"],
    )
    assert summary.attempted == 1 and summary.skipped_budget == 3
    assert "unpriced" in summary.halted


def test_concurrency_is_bounded_and_failed_pairs_do_not_stop_the_run(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=0.02, fail_for={"o__r-2"})
    summary = Harness(team).run(tmp_path, make_tasks(8), concurrency=3)
    assert team.peak == 3
    assert (summary.attempted, summary.errored) == (8, 1)


def test_cancellation_closes_every_open_workspace(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=30)
    h = Harness(team)

    async def scenario():
        task = asyncio.create_task(
            run_benchmark(
                make_tasks(6), [team.name], tmp_path, concurrency=3,
                team_factory=h.team_factory, workspace_factory=h.workspace_factory,
                evaluator=h.evaluator, unpriced_fn=lambda s: [], log=lambda m: None,
            )
        )
        while len(h.workspaces) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(h.workspaces) == 3 and all(w.closed for w in h.workspaces)
    assert not (tmp_path / "results.jsonl").exists()


def test_solve_timeout_is_an_errored_pair_and_the_workspace_is_closed(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=30)
    h = Harness(team)
    summary = h.run(tmp_path, make_tasks(1), solve_timeout_s=0.05)
    assert summary.errored == 1
    record = load_records(tmp_path / "results.jsonl")[0]
    assert record["error"] == "solve timed out after 0.05s" and h.workspaces[0].closed
    assert h.eval_calls == 0


def test_duplicate_team_specs_are_rejected_after_canonicalisation(tmp_path):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    h.teams["solo:worker=claude@x"] = team
    with pytest.raises(ValueError, match="duplicate team"):
        h.run(tmp_path, make_tasks(1), specs=["solo:worker=claude", "solo:worker=claude@x"])


def test_required_secret_keys_follow_the_harnesses_in_use():
    table = {
        "solo:worker=claude": ["claude"],
        "best:workers=codex@gpt-x+claude": ["codex@gpt-x", "claude"],
        "review:coder=sdk@litellm/openrouter/foo/bar,reviewer=sdk@gpt-x": [
            "sdk@litellm/openrouter/foo/bar", "sdk@gpt-x",
        ],
        "solo:worker=sdk@litellm/gemini/g-1": ["sdk@litellm/gemini/g-1"],
        "solo:worker=sdk@litellm/other/m": ["sdk@litellm/other/m"],
    }
    keys = lambda specs: required_secret_keys(specs, harnesses=lambda s: table[s])  # noqa: E731
    assert keys(["solo:worker=claude"]) == ("ANTHROPIC_API_KEY",)
    assert keys(["best:workers=codex@gpt-x+claude"]) == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    assert keys(["review:coder=sdk@litellm/openrouter/foo/bar,reviewer=sdk@gpt-x"]) == (
        "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    )
    assert keys(["solo:worker=sdk@litellm/gemini/g-1"]) == ("GEMINI_API_KEY",)
    assert keys(["solo:worker=sdk@litellm/other/m"]) == ()


def test_gold_validation_lists_instances_whose_reference_patch_resolves(tmp_path):
    seen = []

    async def evaluator(task, patch):
        seen.append(patch)
        return EvalResult(resolved=task.task_id != "o__r-1")

    tasks = make_tasks(3)
    valid = asyncio.run(
        validate_gold(tasks, tmp_path, concurrency=2, evaluator=evaluator, log=lambda m: None)
    )
    assert valid == ["o__r-0", "o__r-2"] and sorted(seen) == ["gold-0", "gold-1", "gold-2"]
    again = asyncio.run(
        validate_gold(tasks, tmp_path, evaluator=evaluator, log=lambda m: None)
    )
    assert again == valid and len(seen) == 3  # graded ids are not repeated
    assert len(load_records(tmp_path / "gold.jsonl")) == 3


def test_tasks_command_prints_the_seeded_sample_without_credentials(capsys):
    from pathlib import Path

    fixture = str(Path(__file__).parent / "fixtures" / "rows.jsonl")
    args = ["tasks", "--dataset", fixture, "--since", "2025-01-01", "--limit", "2", "--seed", "4", "--json"]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    assert capsys.readouterr().out == first
    rows = [json.loads(line) for line in first.splitlines()]
    assert len(rows) == 2 and all(r["created_at"] >= "2025-01-01" for r in rows)
    assert set(rows[0]) >= {"task_id", "repo", "language", "image", "f2p", "p2p"}

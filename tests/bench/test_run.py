import asyncio
import json
from datetime import datetime, timezone

import pytest

from agent.bench.cli import main
from agent.bench.dataset import BenchTask, GradingSpec
from agent.bench.evaluate import EvalResult
from agent.bench.redact import Redactor
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
        self.workspace_fail = set()
        self.diff_fails = False
        self.resolve = resolve
        self.eval_calls = 0
        MemoryWorkspace.created = self.workspaces

    def team_factory(self, spec):
        return self.teams[spec]

    async def workspace_factory(self, task):
        if task.task_id in self.workspace_fail:
            raise RuntimeError("image pull failed")
        return MemoryWorkspace(diff_fails=self.diff_fails)

    async def evaluator(self, task, patch):
        self.eval_calls += 1
        if task.task_id in self.eval_fail:
            raise RuntimeError("sandbox exploded")
        self.graded.append((task.task_id, patch))
        return EvalResult(resolved=self.resolve(task.task_id, patch), log="test log")

    def run(self, out, tasks, specs=None, **kwargs):
        kwargs.setdefault("unpriced_fn", lambda spec: [])
        kwargs.setdefault("log", lambda message: None)
        kwargs.setdefault("redactor", Redactor(host_env=False))
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
    assert rec["isolate_history"] is True and rec["image_head"] == "a" * 40
    assert summary.spent_usd == pytest.approx(3 * 0.5 + 3 * 1.0)
    artifacts = tmp_path / "artifacts" / "review_coder=claude,reviewer=claude" / "o__r-0"
    assert (artifacts / "patch.diff").read_text().startswith("diff --git")
    assert json.loads((artifacts / "runs.json").read_text())["runs"][0]["log"] == "LOG"
    assert (artifacts / "eval.log").read_text() == "test log"


def test_meta_json_keeps_the_first_invocation_and_appends_later_ones_with_the_full_filters(tmp_path):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    filters = {"dataset": "multilang", "since": "2026-07-01", "languages": ["go"], "seed": 7,
               "repos": None, "instance_ids": None, "gold_file": "g.jsonl"}
    h.run(tmp_path, make_tasks(2), run_meta=filters)
    first = json.loads((tmp_path / "meta.json").read_text())
    h.run(tmp_path, make_tasks(3), max_cost_usd=50, run_meta={**filters, "seed": 8})
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["created_at"] == first["created_at"] and meta["runs"][0] == first["runs"][0]
    assert len(meta["runs"]) == 2
    one, two = meta["runs"]
    assert one["task_ids"] == ["o__r-0", "o__r-1"] and two["task_ids"][-1] == "o__r-2"
    assert one["teams"] == [team.name] and one["languages"] == ["go"] and one["gold_file"] == "g.jsonl"
    assert two["seed"] == 8 and two["max_cost_usd"] == 50
    assert meta["datasets"]["multilang"]["revision"]


def test_agents_receive_only_the_visible_task_never_hidden_tests(tmp_path):
    team = FakeTeam("solo:worker=claude")
    Harness(team).run(tmp_path, make_tasks(2))
    for seen in team.tasks_seen:
        assert type(seen) is CodingTask
        assert SECRET_TEST_NAME not in repr(seen) and "gold-" not in repr(seen)
        assert not hasattr(seen, "grading")


def test_resume_skips_finished_pairs_and_retries_only_infrastructure_errors(tmp_path):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    h.workspace_fail = {"o__r-1"}
    first = h.run(tmp_path, make_tasks(3))
    assert (first.attempted, first.errored) == (3, 1)
    records = load_records(tmp_path / "results.jsonl")
    errored = next(r for r in records if r["error"])
    assert errored["task_id"] == "o__r-1" and errored["stage"] == "workspace"
    assert errored["resolved"] is None and errored["usage"] is None  # no agent ever ran

    h.workspace_fail = set()
    team.solved.clear()
    second = h.run(tmp_path, make_tasks(3))
    assert (second.attempted, second.skipped_finished, second.errored) == (1, 2, 0)
    assert team.solved == ["o__r-1"]

    third = h.run(tmp_path, make_tasks(3))
    assert (third.attempted, third.skipped_finished) == (0, 3)
    assert len(load_records(tmp_path / "results.jsonl")) == 4  # append-only; retry superseded the error

    opted_out = Harness(FakeTeam("solo:worker=claude"))
    opted_out.workspace_fail = {"o__r-1"}
    out2 = tmp_path / "second"
    opted_out.run(out2, make_tasks(3))
    opted_out.workspace_fail = set()
    again = opted_out.run(out2, make_tasks(3), retry_errors=False)
    assert again.attempted == 0


@pytest.mark.parametrize("tail", ['{"task_id": "unfinished', '{"usage": {"cost_usd": 1.', '{', ""])
def test_resume_repairs_results_tail_without_losing_finished_pairs(tmp_path, tail):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    h.run(tmp_path, make_tasks(2))
    path = tmp_path / "results.jsonl"
    original = load_records(path)
    contents = path.read_text()
    path.write_text(contents + tail if tail else contents.rstrip("\n"))
    damaged = path.read_bytes()
    assert load_records(path) == original
    assert path.read_bytes() == damaged  # read-only consumers do not repair the file
    team.solved.clear()

    resumed = h.run(tmp_path, make_tasks(3))
    again = h.run(tmp_path, make_tasks(3))

    assert (resumed.attempted, resumed.skipped_finished) == (1, 2)
    assert (again.attempted, again.skipped_finished) == (0, 3)
    assert team.solved == ["o__r-2"]
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[:2] == original
    assert [r["task_id"] for r in records] == [t.task_id for t in make_tasks(3)]


@pytest.mark.parametrize("filename", ["results.jsonl", "gold.jsonl"])
@pytest.mark.parametrize("suffix", ['{"task_id":\n', '{"task_id":\n{"task_id": "later"}\n'])
def test_resume_rejects_malformed_records_before_starting_work(tmp_path, filename, suffix):
    path = tmp_path / filename
    contents = '{"task_id": "o__r-0", "team": "solo:worker=claude", "error": null}\n' + suffix
    path.write_text(contents)
    h = Harness(FakeTeam("solo:worker=claude"))

    with pytest.raises(ValueError):
        load_records(path)
    with pytest.raises(ValueError):
        if filename == "results.jsonl":
            h.run(tmp_path, make_tasks(3))
        else:
            asyncio.run(validate_gold(make_tasks(3), tmp_path, evaluator=h.evaluator))

    assert h.workspaces == [] and h.eval_calls == 0
    assert path.read_text() == contents


def test_a_crashing_team_is_graded_on_what_it_left_instead_of_dropping_the_task(tmp_path):
    team = FakeTeam("solo:worker=claude", fail_for={"o__r-1"}, partial="diff --git a/partial b/partial\n")
    h = Harness(team)
    summary = h.run(tmp_path, make_tasks(3))
    assert (summary.attempted, summary.errored) == (3, 0)
    record = next(r for r in load_records(tmp_path / "results.jsonl") if r["task_id"] == "o__r-1")
    assert record["error"] is None and record["stage"] == "done"
    assert record["meta"]["solve_error"].startswith("RuntimeError")
    assert record["patch"] == "diff --git a/partial b/partial\n"
    assert ("o__r-1", record["patch"]) in h.graded
    assert record["usage"]["cost_usd"] is None  # what the agents spent is unknown
    assert all(w.closed for w in h.workspaces)


def test_grading_errors_regrade_the_saved_patch_without_paying_for_the_agents_again(tmp_path):
    team = FakeTeam("solo:worker=claude", cost=2.0)
    h = Harness(team, eval_fail={"o__r-0"})
    first = h.run(tmp_path, make_tasks(1))
    assert first.errored == 1 and first.spent_usd == 2.0  # the agent cost is kept on the record
    record = load_records(tmp_path / "results.jsonl")[0]
    assert record["stage"] == "evaluate" and record["error"].startswith("evaluate: RuntimeError")
    assert record["patch"]

    team.solved.clear()
    failed_retry = h.run(tmp_path, make_tasks(1))
    assert failed_retry.errored == 1 and failed_retry.spent_usd == 2.0
    assert team.solved == []

    h.eval_fail.clear()
    team.solved.clear()
    second = h.run(tmp_path, make_tasks(1))
    assert team.solved == []  # not re-solved
    final = load_records(tmp_path / "results.jsonl")[-1]
    assert final["error"] is None and final["resolved"] is True
    assert final["usage"]["cost_usd"] == 2.0 and final["resumed_from_error"].startswith("evaluate:")
    assert final["isolate_history"] is True and final["image_head"] == "a" * 40
    assert second.spent_usd == pytest.approx(2.0)  # counted once, not twice

    restarted = h.run(tmp_path, make_tasks(1))
    assert restarted.attempted == 0 and restarted.spent_usd == pytest.approx(2.0)


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


def test_timed_out_solves_are_graded_as_left_and_halt_a_budgeted_run(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=30, partial="diff --git a/half b/half\n")
    h = Harness(team)
    summary = h.run(tmp_path, make_tasks(4), concurrency=1, solve_timeout_s=0.05, max_cost_usd=100)
    # The first timeout leaves an unknown cost: the budget cannot be enforced, nothing else starts.
    assert (summary.attempted, summary.timed_out, summary.errored) == (1, 1, 0)
    assert summary.skipped_budget == 3 and "unknown cost" in summary.halted
    record = load_records(tmp_path / "results.jsonl")[0]
    assert record["error"] is None and record["stage"] == "done"
    assert record["meta"]["timed_out"] is True
    assert record["patch"] == "diff --git a/half b/half\n"
    assert record["usage"]["cost_usd"] is None and record["resolved"] is True
    assert h.graded == [("o__r-0", record["patch"])]
    assert h.workspaces[0].closed

    # Resuming does not forget that: the halt survives a restart, and the pair is not re-run.
    again = h.run(tmp_path, make_tasks(4), concurrency=1, solve_timeout_s=0.05, max_cost_usd=100)
    assert again.attempted == 0 and "unknown cost" in again.halted
    # Without a budget the same timeouts just run, each graded.
    free = Harness(FakeTeam("solo:worker=claude", delay=30, partial="x\n"))
    summary = free.run(tmp_path / "free", make_tasks(3), solve_timeout_s=0.05)
    assert (summary.attempted, summary.timed_out, summary.errored) == (3, 3, 0)


def test_allow_unpriced_budgets_priced_spend_only_and_keeps_running(tmp_path):
    team = FakeTeam("solo:worker=codex", cost=None)
    h = Harness(team)
    summary = h.run(
        tmp_path, make_tasks(4), concurrency=1, max_cost_usd=5, allow_unpriced=True,
        unpriced_fn=lambda spec: ["codex"],
    )
    assert summary.attempted == 4 and summary.halted is None and summary.unpriced_pairs == 4


def test_no_budget_means_no_limit_even_for_unpriced_costs(tmp_path):
    team = FakeTeam("solo:worker=codex", cost=None)
    summary = Harness(team).run(tmp_path, make_tasks(3), unpriced_fn=lambda spec: ["codex"])
    assert summary.attempted == 3 and summary.unpriced_pairs == 3


def test_budgeted_run_refuses_unpriced_harnesses_before_starting_anything(tmp_path):
    team = FakeTeam("solo:worker=codex", cost=None)
    h = Harness(team)
    with pytest.raises(BudgetError):
        h.run(tmp_path, make_tasks(2), max_cost_usd=5, unpriced_fn=lambda spec: ["codex"])
    assert h.workspaces == [] and not (tmp_path / "results.jsonl").exists()


def test_unreadable_workspace_after_a_timeout_is_an_error_with_unknown_cost_that_halts(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=30)
    h = Harness(team)
    h.diff_fails = True
    summary = h.run(tmp_path, make_tasks(3), concurrency=1, solve_timeout_s=0.05, max_cost_usd=100)
    assert (summary.attempted, summary.errored) == (1, 1) and summary.skipped_budget == 2
    record = load_records(tmp_path / "results.jsonl")[0]
    assert record["stage"] == "solve" and "timeout" in record["error"]
    assert record["usage"]["cost_usd"] is None and record["patch"] == ""
    assert h.eval_calls == 0


def test_concurrency_is_bounded_and_failed_pairs_do_not_stop_the_run(tmp_path):
    team = FakeTeam("solo:worker=claude", delay=0.02)
    h = Harness(team)
    h.workspace_fail = {"o__r-2"}
    summary = h.run(tmp_path, make_tasks(8), concurrency=3)
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
                redactor=Redactor(host_env=False),
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
    assert team.active == 0  # the in-flight solves were cancelled with their pairs


def test_duplicate_team_specs_are_rejected_after_canonicalisation(tmp_path):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    h.teams["solo:worker=claude@x"] = team
    with pytest.raises(ValueError):
        h.run(tmp_path, make_tasks(1), specs=["solo:worker=claude", "solo:worker=claude@x"])


def test_secrets_are_scrubbed_from_every_file_the_run_writes_but_grading_sees_originals(tmp_path):
    host_secret = "host-secret-value-0123456789"
    sandbox_secret = "opaque-sandbox-key-abcdefgh"
    pattern_secret = "sk-ant-api03-" + "A1b2C3d4" * 6
    leak = f"env dump: KEY={sandbox_secret} other={pattern_secret} sk-" + "x" * 30
    team = FakeTeam(
        "solo:worker=claude",
        log=f"trajectory {leak}",
        patch=lambda task_id: f"+token = '{sandbox_secret}'\n",
    )
    redactor = Redactor([host_secret], host_env=False)
    redactor.add_env_dump(f"PATH=/usr/bin\nANTHROPIC_API_KEY={sandbox_secret}\nHOME=/root")

    class Leaky(Harness):
        async def evaluator(self, task, patch):
            result = await super().evaluator(task, patch)
            result.log = f"grading log {host_secret}"
            return result

    h = Leaky(team)
    original = h.workspace_factory

    async def factory(task):
        if task.task_id == "o__r-1":
            raise RuntimeError(f"pull failed with token {host_secret}")
        return await original(task)

    h.workspace_factory = factory
    summary = h.run(tmp_path, make_tasks(2), redactor=redactor)
    assert summary.attempted == 2
    # Grading received the real patch; only the stored copy is scrubbed.
    assert h.graded[0][1] == f"+token = '{sandbox_secret}'\n"
    blob = "\n".join(
        p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file()
    )
    for secret in (host_secret, sandbox_secret, pattern_secret, "x" * 30):
        assert secret not in blob
    assert "[REDACTED]" in blob
    records = {r["task_id"]: r for r in load_records(tmp_path / "results.jsonl")}
    assert records["o__r-0"]["patch_redacted"] is True and records["o__r-0"]["redacted"] is True
    assert "[REDACTED]" in records["o__r-1"]["error"]
    clean = Harness(FakeTeam("solo:worker=claude")).run(tmp_path / "clean", make_tasks(1))
    assert clean.records[0]["redacted"] is False and clean.records[0]["patch_redacted"] is False


@pytest.mark.parametrize("patch", ["", "+opaque-sandbox-key-abcdefgh\n"])
def test_unusable_saved_patches_require_another_paid_solve(tmp_path, patch):
    secret = "opaque-sandbox-key-abcdefgh"
    team = FakeTeam("solo:worker=claude", cost=2.0, patch=lambda t: patch)
    redactor = Redactor([secret], host_env=False)
    h = Harness(team, eval_fail={"o__r-0"})
    first = h.run(tmp_path, make_tasks(1), redactor=redactor, max_cost_usd=4)
    assert first.spent_usd == 2.0
    h.eval_fail.clear()
    team.solved.clear()
    second = h.run(tmp_path, make_tasks(3), redactor=redactor, max_cost_usd=4, concurrency=1)
    assert team.solved == ["o__r-0"]  # an empty or scrubbed patch cannot be reused
    assert load_records(tmp_path / "results.jsonl")[-1]["resolved"] is True
    assert second.spent_usd == 4.0 and second.skipped_budget == 2

    restarted = h.run(tmp_path, make_tasks(3), redactor=redactor, max_cost_usd=4, concurrency=1)
    assert restarted.spent_usd == 4.0 and restarted.skipped_budget == 2
    assert restarted.attempted == 0 and restarted.skipped_finished == 1


def test_a_workspace_error_on_retry_does_not_erase_previous_solve_spend(tmp_path):
    team = FakeTeam("solo:worker=claude", cost=2.0, patch=lambda t: "")
    h = Harness(team, eval_fail={"o__r-0"})
    h.run(tmp_path, make_tasks(1))
    h.workspace_fail.add("o__r-0")
    failed_retry = h.run(tmp_path, make_tasks(1))
    assert failed_retry.errored == 1 and failed_retry.spent_usd == 2.0

    h.workspace_fail.clear()
    team.solved.clear()
    restarted = h.run(tmp_path, make_tasks(2), max_cost_usd=2, concurrency=1)
    assert restarted.spent_usd == 2.0 and restarted.skipped_budget == 2
    assert team.solved == []


def test_unknown_solve_cost_still_halts_the_budget_after_a_priced_retry(tmp_path):
    team = FakeTeam("solo:worker=claude", cost=None, patch=lambda t: "")
    h = Harness(team, eval_fail={"o__r-0"})
    h.run(tmp_path, make_tasks(1))
    team.cost = 2.0
    h.eval_fail.clear()
    h.run(tmp_path, make_tasks(1))

    team.solved.clear()
    restarted = h.run(tmp_path, make_tasks(2), max_cost_usd=4, concurrency=1)
    assert restarted.spent_usd == 2.0 and restarted.skipped_budget == 1
    assert restarted.halted and team.solved == []


def test_report_command_covers_only_the_latest_run_of_a_reused_output_dir(tmp_path, capsys):
    old = FakeTeam("solo:worker=codex")
    new = FakeTeam("solo:worker=claude")
    Harness(old).run(tmp_path, make_tasks(3))
    Harness(new).run(tmp_path, make_tasks(2))
    assert main(["report", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "solo:worker=claude" in out and "solo:worker=codex" not in out
    assert "2 of 2 tasks" in out
    assert main(["report", str(tmp_path), "--team", "solo:worker=codex"]) == 0
    assert "solo:worker=codex" in capsys.readouterr().out


def test_required_secret_keys_cover_only_the_clis_that_run_inside_the_sandbox():
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
    assert keys(["best:workers=codex@gpt-x+claude"]) == ("ANTHROPIC_API_KEY", "CODEX_API_KEY")
    # sdk harnesses call their provider from the host: nothing is required in, or exposed to, the
    # sandbox for them.
    assert keys(["review:coder=sdk@litellm/openrouter/foo/bar,reviewer=sdk@gpt-x"]) == ()
    assert keys(["solo:worker=sdk@litellm/gemini/g-1"]) == ()


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


@pytest.mark.parametrize("tail", ['{"task_id": "unfinished', '{"resolved": tru', '{', ""])
def test_gold_resume_repairs_tail_without_repeating_completed_grades(tmp_path, tail):
    h = Harness()

    def run(tasks):
        return asyncio.run(
            validate_gold(tasks, tmp_path, evaluator=h.evaluator, log=lambda m: None)
        )

    run(make_tasks(2))
    path = tmp_path / "gold.jsonl"
    original = load_records(path)
    contents = path.read_text()
    path.write_text(contents + tail if tail else contents.rstrip("\n"))
    h.graded.clear()

    expected = [t.task_id for t in make_tasks(3)]
    assert run(make_tasks(3)) == expected
    assert run(make_tasks(3)) == expected
    assert h.graded == [("o__r-2", "gold-2")]
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[:2] == original
    assert [r["task_id"] for r in records] == expected


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


@pytest.mark.parametrize("option", ["max_cost_usd", "solve_timeout_s"])
@pytest.mark.parametrize("value", [0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_limits_fail_before_factories_or_output(tmp_path, option, value):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid limits must not call factories")

    out = tmp_path / "out"
    with pytest.raises(ValueError, match=option):
        asyncio.run(run_benchmark(
            make_tasks(1), ["solo:worker=claude"], out,
            team_factory=unexpected, unpriced_fn=unexpected, **{option: value},
        ))
    assert not out.exists()


@pytest.mark.parametrize("option", ["--max-cost-usd", "--solve-timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_limits_before_loading_tasks_or_backend(tmp_path, monkeypatch, option, value):
    from agent.bench import cli

    def unexpected(*args, **kwargs):
        pytest.fail("invalid limits must not load tasks or create a backend")

    monkeypatch.setattr(cli, "_select", unexpected)
    monkeypatch.setattr(cli, "_backend", unexpected)
    out = tmp_path / "out"
    assert main(["run", "--team", "solo:worker=claude", "--out", str(out), f"{option}={value}"]) == 2
    assert not out.exists()


@pytest.mark.parametrize("value", [None, 0.25])
def test_optional_positive_fractional_limits_are_accepted(tmp_path, value):
    team = FakeTeam("solo:worker=claude")
    h = Harness(team)
    summary = h.run(tmp_path, make_tasks(1), max_cost_usd=value, solve_timeout_s=value)
    assert summary.attempted == 1 and summary.errored == 0
    assert len(team.solved) == 1 and len(h.graded) == 1

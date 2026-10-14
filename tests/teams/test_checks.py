import os

import pytest

from agent.teams import (
    BestTeam,
    Check,
    CheckHarness,
    CheckResult,
    CodingTask,
    LocalWorktree,
    ReviewTeam,
    SoloTeam,
    SpecError,
    build_team,
    is_priced,
)

from .fakes import FakeHarness, make_repo, read, run, verdict_json, write

TASK = CodingTask("t1", "make solution.txt contain the answer")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    return make_repo(tmp_path / "repo")


def solve(team, repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            return await team.solve(TASK, ws)

    return run(go())


def writer(path, content, name="w"):
    async def script(call):
        await write(call.workspace, path, content)
        return "done"

    return FakeHarness(script, name=name)


def test_solo_records_a_passing_check_run_against_the_final_patch(repo):
    check = Check("grep -q good solution.txt && echo checked")
    result = solve(SoloTeam(writer("solution.txt", "good\n"), check=check), repo)
    recorded = result.meta["check"]
    assert recorded["passed"] is True and recorded["exit_code"] == 0
    assert recorded["command"] == check.command and "checked" in recorded["output_tail"]
    assert recorded["duration_s"] >= 0
    # The check is not a harness run: no extra run, no extra cost.
    assert [r.role for r in result.runs] == ["worker"] and result.usage.cost_usd == pytest.approx(0.01)


def test_a_failing_check_is_recorded_with_the_tail_of_its_output(repo):
    check = Check("echo 'first line'; echo 'boom: assertion failed' >&2; exit 3")
    result = solve(SoloTeam(writer("solution.txt", "bad\n"), check=check), repo)
    recorded = result.meta["check"]
    assert recorded["passed"] is False and recorded["exit_code"] == 3
    assert "boom: assertion failed" in recorded["output_tail"]
    assert result.patch  # the patch is returned either way


def test_the_check_runs_in_a_fork_and_does_not_touch_the_teams_workspace(repo):
    check = Check("echo stray > stray.txt")

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            result = await SoloTeam(writer("solution.txt", "x\n"), check=check).solve(TASK, ws)
            return result, os.path.exists(os.path.join(ws.root, "stray.txt"))

    result, leaked = run(go())
    assert result.meta["check"]["passed"] and not leaked and "stray" not in result.patch


def test_a_check_that_times_out_is_a_failed_check(repo):
    result = solve(SoloTeam(writer("solution.txt", "x\n"), check=Check("sleep 30", timeout=0.5)), repo)
    recorded = result.meta["check"]
    assert recorded["passed"] is False and recorded["exit_code"] is None
    assert "timed out" in recorded["output_tail"]


def test_an_empty_patch_is_not_checked(repo):
    async def nothing(call):
        return "no changes"

    result = solve(SoloTeam(FakeHarness(nothing), check=Check("true")), repo)
    assert result.patch == "" and result.meta["check"]["passed"] is False
    assert result.meta["check"]["exit_code"] is None


def numbered_coder():
    async def script(call):
        await write(call.workspace, "solution.txt", f"attempt {call.number}\n")
        return "done"

    return FakeHarness(script, name="coder")


def test_review_with_check_as_reviewer_loops_until_the_check_passes(repo, tmp_path):
    counter = tmp_path / "runs.txt"
    command = f"echo run >> {counter}; grep -q 'attempt 3' solution.txt || {{ echo 'need attempt 3'; exit 1; }}"
    coder = numbered_coder()
    team = ReviewTeam(coder, CheckHarness(command), rounds=5, check=Check(command))
    result = solve(team, repo)

    assert len(coder.calls) == 3 and "attempt 3" in result.patch
    assert result.meta["scores"] == [0, 0, 10] and result.meta["accepted"] is True
    assert "need attempt 3" in coder.calls[1].prompt  # the check output is the feedback
    assert [r.role for r in result.runs] == ["coder", "reviewer"] * 3
    reviewer_runs = [r for r in result.runs if r.role == "reviewer"]
    assert all(r.harness == "check" and r.usage.cost_usd == 0.0 for r in reviewer_runs)
    assert result.usage.cost_usd == pytest.approx(0.03)
    assert result.meta["check"]["passed"] is True
    # The reviewer's run is the recorded check: three attempts, three runs, none repeated at the end.
    assert len(counter.read_text().split()) == 3


def test_a_check_reviewer_alone_implies_the_check_and_returns_the_last_attempt_when_none_pass(repo):
    coder = numbered_coder()
    result = solve(ReviewTeam(coder, CheckHarness("exit 1"), rounds=2), repo)
    assert len(coder.calls) == 2 and result.meta["accepted"] is False
    assert result.meta["scores"] == [0, 0]
    assert "attempt 2" in result.patch and result.meta["best_round"] == 2
    assert result.meta["check"]["passed"] is False and result.meta["check"]["command"] == "exit 1"


def test_a_check_reviewer_and_the_team_check_must_run_the_same_command():
    with pytest.raises(ValueError, match="same command"):
        ReviewTeam(numbered_coder(), CheckHarness("a"), check=Check("b"))


def test_a_coder_that_stops_changing_anything_is_not_checked_again(repo, tmp_path):
    counter = tmp_path / "runs.txt"
    command = f"echo run >> {counter}; exit 1"

    async def same(call):
        await write(call.workspace, "solution.txt", "same\n")
        return "done"

    result = solve(ReviewTeam(FakeHarness(same, name="coder"), CheckHarness(command), rounds=4), repo)
    assert len(counter.read_text().split()) == 1 and "same" in result.patch


def test_an_llm_reviewer_sees_the_check_result_for_the_patch_it_reviews(repo, tmp_path):
    counter = tmp_path / "runs.txt"

    async def reviewer_script(call):
        return verdict_json(9, passed=True)

    reviewer = FakeHarness(reviewer_script, name="reviewer")
    check = Check(f"echo run >> {counter}; echo 'tests: 2 failed'; exit 1")
    result = solve(ReviewTeam(numbered_coder(), reviewer, rounds=1, check=check), repo)
    prompt = reviewer.calls[0].prompt
    assert "FAILED" in prompt and "tests: 2 failed" in prompt and "attempt 1" in prompt
    assert result.meta["check"]["passed"] is False
    assert len(counter.read_text().split()) == 1  # the final record reuses the run the reviewer saw


def test_check_output_cannot_close_the_code_fence_around_it(repo):
    check = Check("echo 'start'; echo '```'; echo 'after fence'; exit 1")
    recorded = solve(SoloTeam(writer("solution.txt", "x\n"), check=check), repo).meta["check"]
    text = CheckResult(**recorded).describe()
    outer = [line for line in text.splitlines() if line == "````"]
    assert len(outer) == 2 and "after fence" in text


def test_stdout_and_stderr_are_kept_on_separate_lines(repo):
    result = solve(SoloTeam(writer("solution.txt", "x\n"), check=Check("printf out; printf err >&2")), repo)
    assert result.meta["check"]["output_tail"] == "out\nerr"


def judge_scoring(scores):
    async def script(call):
        text = (await read(call.workspace, "out.txt")).strip()
        return verdict_json(scores[text], feedback=text)

    return FakeHarness(script, name="judge")


def test_best_ranks_candidates_failing_the_check_below_every_passing_one(repo):
    workers = [writer("out.txt", f"{v}\n", name=f"w{v}") for v in "abc"]
    scores = {"a": 9, "b": 5, "c": 7}  # the judge likes the failing candidate best
    check = Check("grep -q -e b -e c out.txt")

    control = solve(BestTeam(workers, judge_scoring(scores)), repo)
    assert control.meta["chosen"] == 1 and "check" not in control.meta

    result = solve(BestTeam(workers, judge_scoring(scores), check=check), repo)
    assert result.meta["chosen"] == 3 and "+c" in result.patch
    assert result.meta["scores"] == [9, 5, 7]  # raw judge scores are kept
    assert [c["check"]["passed"] for c in result.meta["candidates"]] == [False, True, True]
    assert result.meta["check"]["passed"] is True


def test_best_orders_by_score_when_every_candidate_fails_the_check(repo):
    workers = [writer("out.txt", f"{v}\n", name=f"w{v}") for v in "ab"]
    result = solve(BestTeam(workers, judge_scoring({"a": 3, "b": 8}), check=Check("exit 1")), repo)
    assert result.meta["chosen"] == 2 and result.meta["check"]["passed"] is False


def test_best_with_check_as_judge_keeps_the_candidate_that_passes(repo):
    workers = [writer("out.txt", f"{v}\n", name=f"w{v}") for v in "abc"]
    command = "grep -q c out.txt"
    result = solve(BestTeam(workers, CheckHarness(command), check=Check(command)), repo)
    assert result.meta["chosen"] == 3 and result.meta["scores"] == [0, 0, 10]
    assert [r.role for r in result.runs] == ["worker", "judge"] * 3
    assert result.usage.cost_usd == pytest.approx(0.03) and result.meta["check"]["passed"] is True


def test_check_harness_is_priced_and_valid_only_as_reviewer_or_judge():
    assert is_priced("check")
    team = build_team("review:coder=claude,reviewer=check", check="true")
    assert team.name == "review:coder=claude,reviewer=check,rounds=3"
    assert isinstance(team.reviewer, CheckHarness) and team.check.command == "true"
    assert build_team(team.name, check="true").name == team.name
    best = build_team("best:workers=claude+codex,judge=check", check="make test")
    assert isinstance(best.judge, CheckHarness) and best.judge.command == "make test"
    assert build_team("solo:worker=claude", check="true").check == Check("true")
    assert build_team("solo:worker=claude").check is None


@pytest.mark.parametrize(
    "spec",
    [
        "review:coder=claude,reviewer=check",
        "best:workers=claude+codex,judge=check",
    ],
)
def test_check_harness_without_a_check_command_is_a_spec_error(spec):
    with pytest.raises(SpecError, match="check command"):
        build_team(spec)


@pytest.mark.parametrize(
    "spec",
    [
        "solo:worker=check",
        "review:coder=check,reviewer=claude",
        "best:workers=check+claude",
        "review:coder=claude,reviewer=check@haiku",
    ],
)
def test_check_is_rejected_where_a_coding_harness_is_required(spec):
    with pytest.raises(SpecError):
        build_team(spec, check="true")


def test_an_empty_check_command_is_a_spec_error():
    with pytest.raises(SpecError, match="empty"):
        build_team("solo:worker=claude", check="  ")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from agent.teams import spec as spec_module

    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(repo)
    worker = writer("solution.txt", "done\n", name="solo")
    real = spec_module.build_harness
    monkeypatch.setattr(
        spec_module, "build_harness", lambda s, **kw: worker if s == "claude" else real(s, **kw)
    )
    return tmp_path


def _record(tmp_path):
    import json

    (path,) = (tmp_path / "cache" / "multi-agents" / "runs").glob("*/*/record.json")
    return json.loads(path.read_text())


def test_cli_check_flag_is_recorded_and_printed(cli_env, capsys):
    from agent.teams import cli

    code = cli.main(["run", "--team", "solo:worker=claude", "--check", "grep -q done solution.txt", "x"])
    out = capsys.readouterr().out
    assert code == 0 and "check: passed" in out
    record = _record(cli_env)
    assert record["check"]["passed"] is True and record["check"]["command"] == "grep -q done solution.txt"
    assert record["meta"]["check"] == record["check"]


def test_cli_prints_a_failing_check_and_still_returns_the_patch(cli_env, capsys):
    from agent.teams import cli

    code = cli.main(["run", "--team", "solo:worker=claude", "--check", "echo broken; exit 2", "x"])
    out = capsys.readouterr().out
    assert code == 0 and "check: failed" in out and "exit 2" in out and "broken" in out
    assert _record(cli_env)["check"]["passed"] is False


def test_cli_without_check_has_no_check_record(cli_env, capsys):
    from agent.teams import cli

    assert cli.main(["run", "--team", "solo:worker=claude", "x"]) == 0
    assert "check" not in _record(cli_env) and "check:" not in capsys.readouterr().out


def test_cli_rejects_a_check_reviewer_without_a_check_command(cli_env, capsys):
    from agent.teams import cli

    assert cli.main(["run", "--team", "review:coder=claude,reviewer=check", "x"]) == 2
    assert "check command" in capsys.readouterr().out


def test_best_with_two_passing_candidates_takes_the_higher_judge_score_then_the_earliest(repo):
    workers = [writer("out.txt", f"{v}\n", name=f"w{v}") for v in "abc"]
    check = Check("grep -q -e a -e b -e c out.txt")
    result = solve(BestTeam(workers, judge_scoring({"a": 4, "b": 6, "c": 6}), check=check), repo)
    assert result.meta["chosen"] == 2  # all pass; 6 beats 4, the earlier 6 beats the later


def test_cli_explains_an_empty_check_command(cli_env, capsys):
    from agent.teams import cli

    assert cli.main(["run", "--team", "solo:worker=claude", "--check", "  ", "x"]) == 2
    out = capsys.readouterr().out
    assert "--check" in out and "empty" in out and "--team" not in out

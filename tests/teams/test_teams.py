import os

import pytest

from agent.teams import BestTeam, CodingTask, LocalWorktree, ReviewTeam, SoloTeam

from .fakes import FakeHarness, make_repo, read, run, verdict_json, write

TASK = CodingTask("t1", "make solution.txt contain the answer")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    return make_repo(tmp_path / "repo")


def solve(team, repo, task=TASK):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            result = await team.solve(task, ws)
            return result, ws.root, await ws.diff()

    return run(go())


def numbered_coder(name="coder", same_every_time=False):
    async def script(call):
        await write(call.workspace, "solution.txt", "attempt 1\n" if same_every_time else f"attempt {call.number}\n")
        return "done"

    return FakeHarness(script, name=name)


def scoring_reviewer(scores, name="reviewer", prefix="fix"):
    async def script(call):
        n = int((await read(call.workspace, "solution.txt")).split()[1])
        return verdict_json(scores[n], passed=scores[n] >= 9, feedback=f"{prefix}-{n}")

    return FakeHarness(script, name=name)


def test_review_returns_the_best_patch_and_feeds_feedback_back_to_the_coder(repo):
    coder, reviewer = numbered_coder(), scoring_reviewer({1: 4, 2: 7, 3: 5})
    result, root, _ = solve(ReviewTeam(coder, reviewer, rounds=3), repo)

    assert "attempt 2" in result.patch and "attempt 3" not in result.patch
    assert result.meta["scores"] == [4, 7, 5] and result.meta["best_round"] == 2
    assert [r.role for r in result.runs] == ["coder", "reviewer"] * 3
    # The revision prompt carries the reviewer's feedback and the coder resumes its session.
    second = coder.calls[1]
    assert "fix-1" in second.prompt and "4.0/10" in second.prompt and TASK.instruction in second.prompt
    assert second.session_id == "session-coder" and coder.calls[0].session_id is None
    assert result.usage.cost_usd == pytest.approx(0.06)


def test_review_stops_as_soon_as_the_reviewer_passes(repo):
    coder, reviewer = numbered_coder(), scoring_reviewer({1: 3, 2: 9, 3: 9})
    result, *_ = solve(ReviewTeam(coder, reviewer, rounds=5), repo)
    assert result.meta["rounds"] == 2 and result.meta["accepted"] is True
    assert len(coder.calls) == 2 and "attempt 2" in result.patch


def test_review_stops_when_the_coder_stops_changing_anything(repo):
    coder, reviewer = numbered_coder(same_every_time=True), scoring_reviewer({1: 3})
    result, *_ = solve(ReviewTeam(coder, reviewer, rounds=4), repo)
    assert len(coder.calls) == 2 and len(reviewer.calls) == 1  # no second review of an identical patch
    assert "attempt 1" in result.patch


def test_unparseable_review_is_recorded_and_does_not_sink_the_team(repo):
    async def lgtm(call):
        return "looks good to me"

    result, *_ = solve(ReviewTeam(numbered_coder(), FakeHarness(lgtm, name="reviewer"), rounds=2), repo)
    assert "attempt 1" in result.patch  # all scores tie at 0: earliest wins
    assert result.meta["scores"] == [0, 0]
    failures = result.meta["failures"]
    assert len(failures) == 2 and all(f["role"] == "reviewer" and "VerdictParseError" in f["error"] for f in failures)
    assert [r.role for r in result.runs].count("reviewer") == 2


def test_reviewer_cannot_mutate_the_workers_workspace(repo):
    async def meddling(call):
        await write(call.workspace, "tamper.txt", "x")
        await write(call.workspace, "solution.txt", "attempt 99\n")
        return verdict_json(5)

    reviewer = FakeHarness(meddling, name="reviewer")
    result, root, final_diff = solve(ReviewTeam(numbered_coder(), reviewer, rounds=1), repo)
    assert reviewer.roots[0] != root and not os.path.exists(reviewer.roots[0])  # fork, closed
    assert "tamper" not in result.patch and "tamper" not in final_diff
    assert "attempt 99" not in final_diff


def worker_writing(content, name):
    async def script(call):
        await write(call.workspace, "out.txt", content)
        return "done"

    return FakeHarness(script, name=name)


def judge_scoring(scores, name="judge"):
    async def script(call):
        text = await read(call.workspace, "out.txt")
        return verdict_json(scores[text.strip()], feedback=text.strip())

    return FakeHarness(script, name=name)


def test_best_selects_the_highest_score_and_survives_a_failing_worker(repo):
    async def crash(call):
        raise RuntimeError("worker exploded")

    workers = [worker_writing("a\n", "w1"), FakeHarness(crash, name="w2"), worker_writing("c\n", "w3")]
    judge = judge_scoring({"a": 3, "c": 8})
    result, *_ = solve(BestTeam(workers, judge), repo)

    assert "+c" in result.patch and "+a" not in result.patch
    assert result.meta["scores"] == [3, None, 8] and result.meta["chosen"] == 3
    assert [f["candidate"] for f in result.meta["failures"]] == [2]
    assert [r.role for r in result.runs] == ["worker", "judge", "worker", "judge"]
    runs_by_candidate = {c["candidate"]: [result.runs[i].role for i in c["runs"]] for c in result.meta["candidates"]}
    assert runs_by_candidate == {1: ["worker", "judge"], 2: [], 3: ["worker", "judge"]}
    # Every worker and judge ran in its own fork, all closed afterwards.
    roots = [r for h in (*workers, judge) for r in h.roots]
    assert len(set(roots)) == len(roots) and not any(os.path.exists(r) for r in roots)


def test_best_ties_pick_the_earliest_candidate(repo):
    workers = [worker_writing("a\n", "w1"), worker_writing("b\n", "w2")]
    result, *_ = solve(BestTeam(workers, judge_scoring({"a": 6, "b": 6})), repo)
    assert result.meta["chosen"] == 1 and "+a" in result.patch


def test_best_with_an_unusable_judge_still_returns_the_earliest_patch(repo):
    async def silent(call):
        return "no verdict"

    workers = [worker_writing("a\n", "w1"), worker_writing("b\n", "w2")]
    result, *_ = solve(BestTeam(workers, FakeHarness(silent, name="judge")), repo)
    assert result.meta["scores"] == [0, 0] and "+a" in result.patch
    assert {f["role"] for f in result.meta["failures"]} == {"judge"}


def test_best_with_every_worker_failing_returns_an_empty_patch(repo):
    async def crash(call):
        raise RuntimeError("nope")

    result, *_ = solve(BestTeam([FakeHarness(crash), FakeHarness(crash)], judge_scoring({})), repo)
    assert result.patch == "" and result.meta["chosen"] is None and "error" in result.meta


def test_solo_returns_the_workers_patch_and_records_failures(repo):
    result, *_ = solve(SoloTeam(worker_writing("z\n", "solo")), repo)
    assert "+z" in result.patch and [r.role for r in result.runs] == ["worker"]

    async def crash(call):
        raise RuntimeError("down")

    result, *_ = solve(SoloTeam(FakeHarness(crash)), repo)
    assert result.patch == "" and result.runs == [] and "down" in result.meta["failures"][0]["error"]

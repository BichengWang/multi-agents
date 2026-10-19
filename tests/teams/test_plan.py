import asyncio
import json
import os
from io import StringIO

import pytest
from rich.console import Console

from agent.teams import (
    Check,
    CodingTask,
    ExecResult,
    LocalWorktree,
    PlanTeam,
    SpecError,
    build_team,
    parse_team_spec,
)
from agent.teams.cli import render_summary
from agent.teams.harnesses.check import CheckHarness
from agent.teams.plan import PLAN_SCHEMA, PlanParseError, Subtask, _apply_3way, _SubtaskState, parse_plan
from agent.teams.topologies import MAX_PATCH_IN_PROMPT

from .fakes import FakeHarness, ScriptedWorkspace, git, make_repo, read, run, verdict_json, write

TASK = CodingTask("t1", "add modules a and b")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    return make_repo(tmp_path / "repo")


def solve(team, repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            result = await team.solve(TASK, ws)
            return result, ws.root

    return run(go())


def plan_json(*titles, paths=None):
    return json.dumps(
        {
            "subtasks": [
                {"title": t, "instruction": f"do {t}", **({"paths": paths[i]} if paths else {})}
                for i, t in enumerate(titles)
            ]
        }
    )


def planner_saying(text, name="planner"):
    async def script(call):
        await write(call.workspace, "planner-scratch.txt", "x")  # a planner that ignores read-only
        return f"Here is the plan:\n```json\n{text}\n```"

    return FakeHarness(script, name=name)


def worker_writing(files, name="w", fail=False):
    """A worker that writes ``files`` (path -> content) into its fork."""

    async def script(call):
        if fail:
            raise RuntimeError("worker exploded")
        for path, content in files.items():
            await write(call.workspace, path, content)
        return "done"

    return FakeHarness(script, name=name)


def by_role(result, role):
    return [r for r in result.runs if r.role == role]


# --- plan parsing -----------------------------------------------------------------------------


def test_parse_plan_takes_the_last_plan_object_in_the_reply():
    text = (
        'First idea {"subtasks": []}. Final:\n```json\n'
        '{"subtasks": [{"title": " A ", "instruction": "do a", "paths": ["a.py", " "]},'
        ' {"title": "B", "instruction": "do b"}]}\n```'
    )
    plan = parse_plan(text, 4)
    assert [(s.title, s.instruction, s.paths) for s in plan] == [("A", "do a", ("a.py",)), ("B", "do b", ())]


@pytest.mark.parametrize(
    "text",
    [
        "I would just do it myself.",
        '{"subtasks": []}',
        '{"subtasks": "a"}',
        '{"subtasks": [{"title": "A"}]}',
        '{"subtasks": [{"title": "", "instruction": "x"}]}',
        '{"subtasks": [{"title": "A", "instruction": "x", "paths": "a.py"}]}',
        '{"subtasks": ["A"]}',
        plan_json("a", "b", "c"),
    ],
)
def test_parse_plan_rejects_invalid_plans(text):
    with pytest.raises(PlanParseError):
        parse_plan(text, 2)


def test_a_replan_may_have_no_subtasks_but_a_first_plan_may_not():
    empty = '{"subtasks": []}'
    assert parse_plan(empty, 3, min_tasks=0) == []
    with pytest.raises(PlanParseError, match="expected 1 to 3 subtasks, got 0"):
        parse_plan(empty, 3)
    with pytest.raises(PlanParseError, match="expected 0 to 1 subtasks, got 2"):
        parse_plan(plan_json("a", "b"), 1, min_tasks=0)


# --- topology ---------------------------------------------------------------------------------


def test_an_unparseable_plan_is_recorded_and_the_first_worker_solves_the_whole_task(repo):
    w1, w2 = worker_writing({"all.txt": "x\n"}, "w1"), worker_writing({"never.txt": "x\n"}, "w2")
    result, _ = solve(PlanTeam(planner_saying("no json here"), [w1, w2]), repo)

    assert "all.txt" in result.patch and not w2.calls
    assert TASK.instruction in w1.calls[0].prompt
    assert [r.role for r in result.runs] == ["planner", "worker"]
    (failure,) = result.meta["failures"]
    assert failure["role"] == "planner" and "PlanParseError" in failure["error"]
    assert result.meta["fallback"]["to"] == "solo"


def test_a_planner_that_crashes_falls_back_to_solo(repo):
    async def crash(call):
        raise RuntimeError("planner down")

    w1 = worker_writing({"all.txt": "x\n"}, "w1")
    result, _ = solve(PlanTeam(FakeHarness(crash, name="planner"), [w1]), repo)
    assert "all.txt" in result.patch and "planner down" in result.meta["failures"][0]["error"]
    assert [r.role for r in result.runs] == ["worker"]


def test_a_plan_with_more_subtasks_than_max_tasks_is_a_planner_failure(repo):
    w1 = worker_writing({"all.txt": "x\n"}, "w1")
    result, _ = solve(PlanTeam(planner_saying(plan_json("a", "b", "c")), [w1], max_tasks=2), repo)
    assert "expected 1 to 2 subtasks" in result.meta["failures"][0]["error"] and "all.txt" in result.patch


def test_a_single_subtask_behaves_like_solo_with_the_first_worker(repo):
    w1, w2 = worker_writing({"all.txt": "x\n"}, "w1"), worker_writing({"never.txt": "x\n"}, "w2")
    result, _ = solve(PlanTeam(planner_saying(plan_json("everything")), [w1, w2]), repo)
    assert "all.txt" in result.patch and not w2.calls and TASK.instruction in w1.calls[0].prompt
    assert [r.role for r in result.runs] == ["planner", "worker"]
    assert result.meta["fallback"]["reason"] == "the plan has a single subtask"
    assert "failures" not in result.meta


def test_workers_run_in_parallel_in_separate_forks_and_clean_patches_merge_without_the_integrator(repo):
    async def go():
        arrived = {"a": asyncio.Event(), "b": asyncio.Event()}

        def worker(me, other, files, name):
            async def script(call):
                arrived[me].set()
                # Neither finishes until the other has started: only possible if they run concurrently.
                await asyncio.wait_for(arrived[other].wait(), timeout=20)
                for path, content in files.items():
                    await write(call.workspace, path, content)
                return "done"

            return FakeHarness(script, name=name)

        w1 = worker("a", "b", {"a.txt": "module a\n"}, "w1")
        w2 = worker("b", "a", {"b.txt": "module b\n"}, "w2")
        planner = planner_saying(plan_json("module a", "module b", paths=[["a.txt"], ["b.txt"]]))
        async with await LocalWorktree.create(repo) as ws:
            result = await PlanTeam(planner, [w1, w2]).solve(TASK, ws)
            return result, ws.root, planner, w1, w2

    result, root, planner, w1, w2 = run(go())

    assert "module a" in result.patch and "module b" in result.patch
    assert "planner-scratch" not in result.patch  # the planner's fork is discarded
    assert [r.role for r in result.runs] == ["planner", "worker", "worker"]
    roots = [planner.roots[0], w1.roots[0], w2.roots[0]]
    assert len({*roots, root}) == 4 and not any(os.path.exists(r) for r in roots)
    # Each worker is told the overall task, its own subtask, and to leave the others alone.
    prompt = w1.calls[0].prompt
    assert TASK.instruction in prompt and "do module a" in prompt and "a.txt" in prompt
    assert "module b" in prompt and "do module b" not in prompt
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "applied"]
    assert result.meta["integration"]["integrator_runs"] == 0
    assert [result.runs[i].harness for s in result.meta["subtasks"] for i in s["runs"]] == ["w1", "w2"]


def test_subtasks_are_assigned_round_robin_over_the_workers(repo):
    def numbered(name):
        async def script(call):
            number = call.prompt.split("Your subtask (")[1].split(" of")[0]
            await write(call.workspace, f"s{number}.txt", f"{number}\n")
            return "done"

        return FakeHarness(script, name=name)

    w1, w2 = numbered("w1"), numbered("w2")
    result, _ = solve(PlanTeam(planner_saying(plan_json("one", "two", "three")), [w1, w2]), repo)
    assert len(w1.calls) == 2 and len(w2.calls) == 1
    assert [s["worker"] for s in result.meta["subtasks"]] == ["w1@m", "w2@m", "w1@m"]
    assert all(f"s{n}.txt" in result.patch for n in (1, 2, 3))


def test_edits_to_different_parts_of_one_file_merge_cleanly(repo):
    lines = [f"line {i}\n" for i in range(1, 21)]
    (repo / "big.txt").write_text("".join(lines))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "big")

    def edited(index, text):
        out = list(lines)
        out[index] = text
        return "".join(out)

    w1 = worker_writing({"big.txt": edited(1, "top changed\n")}, "w1")
    w2 = worker_writing({"big.txt": edited(4, "bottom changed\n")}, "w2")  # shares context lines: plain apply fails
    result, _ = solve(PlanTeam(planner_saying(plan_json("top", "bottom")), [w1, w2]), repo)
    assert "+top changed" in result.patch and "+bottom changed" in result.patch
    assert result.meta["integration"]["conflicts"] == [] and not by_role(result, "integrator")


def test_conflicting_patches_are_routed_to_the_integrator_in_the_teams_workspace(repo):
    seen = {}

    async def integrate(call):
        seen["root"] = call.workspace.root
        seen["a"] = await read(call.workspace, "a.txt")
        seen["prompt"] = call.prompt
        await write(call.workspace, "a.txt", "one and two\n")
        return "merged"

    w1 = worker_writing({"a.txt": "one\n", "x.txt": "x\n"}, "w1")
    w2 = worker_writing({"a.txt": "two\n", "y.txt": "y\n"}, "w2")
    integrator = FakeHarness(integrate, name="integrator")
    team = PlanTeam(planner_saying(plan_json("first", "second")), [w1, w2], integrator)
    result, root = solve(team, repo)

    assert seen["root"] == root
    # The clean patch stays, the conflicting one was rolled back: no conflict markers either.
    assert seen["a"] == "one\n"
    prompt = seen["prompt"]
    assert TASK.instruction in prompt and "Subtask 2 (second)" in prompt and "could NOT be merged" in prompt
    assert "+two" in prompt and "+one" in prompt  # all patches are shown
    assert "one and two" in result.patch and "x.txt" in result.patch and "y.txt" not in result.patch
    assert [r.role for r in result.runs] == ["planner", "worker", "worker", "integrator"]
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "conflict"]
    assert result.meta["integration"]["conflicts"] == [2] and result.meta["integration"]["integrator_runs"] == 1


def test_the_integrator_defaults_to_the_first_worker(repo):
    conflicting = [worker_writing({"a.txt": "one\n"}, "w1"), worker_writing({"a.txt": "two\n"}, "w2")]
    solve(PlanTeam(planner_saying(plan_json("p", "q")), conflicting), repo)
    assert [c.role for c in conflicting[0].calls] == ["worker", "integrator"]
    assert conflicting[0].calls[1].prompt.startswith("You are the integrator")
    assert len(conflicting[1].calls) == 1


def test_a_failed_subtask_is_recorded_and_the_integrator_completes_it_when_replans_is_zero(repo):
    async def integrate(call):
        await write(call.workspace, "b.txt", "module b\n")
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1 = worker_writing({"a.txt": "module a\n"}, "w1")
    w2 = worker_writing({}, "w2", fail=True)
    team = PlanTeam(planner_saying(plan_json("module a", "module b")), [w1, w2], integrator, replans=0)
    result, _ = solve(team, repo)

    assert "module a" in result.patch and "module b" in result.patch
    failure = next(f for f in result.meta["failures"] if f["role"] == "worker")
    assert failure["subtask"] == 2 and "worker exploded" in failure["error"]
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "failed"]
    prompt = integrator.calls[0].prompt
    assert "Subtask 2 (module b): MISSING" in prompt and "do module b" in prompt
    assert [r.role for r in result.runs] == ["planner", "worker", "integrator"]


def test_a_worker_that_changes_nothing_counts_as_a_missing_subtask_when_replans_is_zero(repo):
    async def integrate(call):
        await write(call.workspace, "b.txt", "module b\n")
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1, w2 = worker_writing({"a.txt": "a\n"}, "w1"), worker_writing({}, "w2")
    result, _ = solve(
        PlanTeam(planner_saying(plan_json("a", "b")), [w1, w2], integrator, replans=0), repo
    )
    assert result.meta["subtasks"][1]["status"] == "empty" and "b.txt" in result.patch
    assert "no changes" in result.meta["failures"][0]["error"]


def test_a_failing_check_after_integration_gives_the_integrator_one_fix_pass(repo):
    async def fix(call):
        await write(call.workspace, "b.txt", "fixed\n")
        return "fixed"

    integrator = FakeHarness(fix, name="integrator")
    w1, w2 = worker_writing({"a.txt": "a\n"}, "w1"), worker_writing({"b.txt": "broken\n"}, "w2")
    check = Check("grep -q fixed b.txt || { echo 'b.txt is not fixed'; exit 1; }")
    team = PlanTeam(planner_saying(plan_json("a", "b")), [w1, w2], integrator, check=check)
    result, _ = solve(team, repo)

    assert len(integrator.calls) == 1 and "b.txt is not fixed" in integrator.calls[0].prompt
    assert TASK.instruction in integrator.calls[0].prompt
    assert result.meta["check"]["passed"] is True and result.meta["check_before_fix"]["passed"] is False
    assert "fixed" in result.patch and [r.role for r in result.runs][-1] == "integrator"


def test_the_fix_pass_happens_once_even_if_the_check_still_fails(repo):
    async def noop(call):
        return "tried"

    integrator = FakeHarness(noop, name="integrator")
    w1, w2 = worker_writing({"a.txt": "a\n"}, "w1"), worker_writing({"b.txt": "b\n"}, "w2")
    team = PlanTeam(planner_saying(plan_json("a", "b")), [w1, w2], integrator, check=Check("exit 1"))
    result, _ = solve(team, repo)
    assert len(integrator.calls) == 1 and result.meta["check"]["passed"] is False
    assert "a.txt" in result.patch and "b.txt" in result.patch


def test_a_passing_check_needs_no_integrator(repo):
    integrator = FakeHarness(None, name="integrator")
    w1, w2 = worker_writing({"a.txt": "a\n"}, "w1"), worker_writing({"b.txt": "b\n"}, "w2")
    team = PlanTeam(planner_saying(plan_json("a", "b")), [w1, w2], integrator, check=Check("test -f b.txt"))
    result, _ = solve(team, repo)
    assert not integrator.calls and result.meta["check"]["passed"] and "check_before_fix" not in result.meta


def test_the_planner_gets_the_json_schema_when_its_harness_supports_it(repo):
    captured = {}

    class SchemaHarness(FakeHarness):
        supports_output_schema = True

        async def run(self, workspace, prompt, *, role="worker", session_id=None, timeout=None, output_schema=None):
            captured["schema"] = output_schema
            return await super().run(workspace, prompt, role=role, session_id=session_id, timeout=timeout)

    async def say(call):
        return plan_json("a", "b")

    w1, w2 = worker_writing({"a.txt": "a\n"}, "w1"), worker_writing({"b.txt": "b\n"}, "w2")
    solve(PlanTeam(SchemaHarness(say, name="planner"), [w1, w2]), repo)
    assert captured["schema"] is PLAN_SCHEMA and PLAN_SCHEMA["required"] == ["subtasks"]


def test_cancelling_a_plan_team_removes_the_forks_of_every_running_worker(repo):
    async def sleeper(call):
        await call.workspace.exec(["sh", "-c", "echo up > started; sleep 300"])
        return "unreachable"

    workers = [FakeHarness(sleeper, name="w1"), FakeHarness(sleeper, name="w2")]
    planner = planner_saying(plan_json("a", "b"))

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            task = asyncio.create_task(PlanTeam(planner, workers).solve(TASK, ws))
            for _ in range(200):
                await asyncio.sleep(0.05)
                roots = [r for h in workers for r in h.roots]
                if len(roots) == 2 and all(os.path.exists(os.path.join(r, "started")) for r in roots):
                    break
            else:
                raise AssertionError("workers never started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Checked while the origin worktree is still open: its own sweep must not be what cleaned up.
            roots = [r for h in (planner, *workers) for r in h.roots]
            assert len(roots) == 3 and not any(os.path.exists(r) for r in roots)

    run(go())


# --- re-planning ------------------------------------------------------------------------------


def planner_replying(*texts, name="planner"):
    """A planner that answers its n-th call with ``texts[n-1]`` (the last one repeats).

    Returns the harness and, per call, the files it could see in its fork.
    """
    seen = []

    async def script(call):
        await write(call.workspace, "planner-scratch.txt", "x")
        seen.append((await call.workspace.exec(["ls"])).stdout.split())
        return f"Here is the plan:\n```json\n{texts[min(call.number, len(texts)) - 1]}\n```"

    return FakeHarness(script, name=name), seen


def titled_worker(name, fail_calls=(), always_fail=False):
    """A worker that writes ``<subtask title>.txt``; it raises on the calls numbered in ``fail_calls``."""

    async def script(call):
        if always_fail or call.number in fail_calls:
            raise RuntimeError("worker exploded")
        title = call.prompt.split("): ", 1)[1].split("\n")[0]
        await write(call.workspace, f"{title}.txt", f"{name}\n")
        return "done"

    return FakeHarness(script, name=name)


def test_a_failed_subtask_is_replanned_and_the_replacement_is_merged_without_the_integrator(repo):
    planner, seen = planner_replying(plan_json("a", "b"), plan_json("a again"))
    w1, w2 = titled_worker("w1", always_fail=True), titled_worker("w2")
    integrator = FakeHarness(None, name="integrator")
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator), repo)

    assert "a again.txt" in result.patch and "b.txt" in result.patch and "a.txt" not in result.patch
    assert "planner-scratch" not in result.patch and not integrator.calls
    assert [(r.role, r.harness) for r in result.runs] == [
        ("planner", "planner"),
        ("worker", "w2"),
        ("planner", "planner"),
        ("worker", "w2"),
    ]
    subtasks = result.meta["subtasks"]
    assert [s["status"] for s in subtasks] == ["replanned", "applied", "applied"]
    assert [s["round"] for s in subtasks] == [0, 0, 1]
    assert [s["worker"] for s in subtasks] == ["w1@m", "w2@m", "w2@m"]  # round 1 rotates the assignment
    assert [s["runs"] for s in subtasks] == [[], [1], [3]]
    assert result.meta["replans"] == [{"round": 1, "failed": [1], "subtasks": [3]}]
    assert result.meta["integration"]["integrator_runs"] == 0 and result.meta["integration"]["missing"] == []
    (failure,) = result.meta["failures"]
    assert failure["subtask"] == 1 and "worker exploded" in failure["error"]

    replan_prompt = planner.calls[1].prompt
    assert "Merged subtasks (already in the working tree):\n- 2. b\n" in replan_prompt
    assert "- 1. a: RuntimeError: worker exploded\n  Its instruction was: do a" in replan_prompt
    assert TASK.instruction in replan_prompt and "between 0 and 4" in replan_prompt
    assert "b.txt" not in seen[0] and "b.txt" in seen[1]  # the replan planner sees what is merged
    assert "a again" in w2.calls[1].prompt and "(1 of 1)" in w2.calls[1].prompt


def test_replacement_subtasks_rotate_over_the_workers_by_round(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("c", "d"))
    w1, w2 = titled_worker("w1", fail_calls=(1,)), titled_worker("w2", fail_calls=(1,))
    result, _ = solve(PlanTeam(planner, [w1, w2]), repo)

    subtasks = result.meta["subtasks"]
    assert [s["status"] for s in subtasks] == ["replanned", "replanned", "applied", "applied"]
    assert [s["worker"] for s in subtasks] == ["w1@m", "w2@m", "w2@m", "w1@m"]  # workers[(i + round) % 2]
    assert [s["round"] for s in subtasks] == [0, 0, 1, 1]
    assert result.meta["replans"] == [{"round": 1, "failed": [1, 2], "subtasks": [3, 4]}]
    assert "c.txt" in result.patch and "d.txt" in result.patch
    assert [r.role for r in result.runs] == ["planner", "planner", "worker", "worker"]


def test_a_replan_is_bounded_by_replans_and_the_integrator_completes_what_is_still_missing(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("c"), plan_json("d"))

    async def integrate(call):
        await write(call.workspace, "c.txt", "integrator\n")
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator, replans=1), repo)

    assert len(planner.calls) == 2  # the first plan and one replan
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "replanned", "failed"]
    assert [s["worker"] for s in result.meta["subtasks"]] == ["w1@m", "w2@m", "w2@m"]
    assert result.meta["integration"]["missing"] == [3] and result.meta["integration"]["integrator_runs"] == 1
    prompt = integrator.calls[0].prompt
    assert "Subtask 2 (b): FAILED" in prompt and "handed back to the planner" in prompt
    assert "Subtask 3 (c): MISSING" in prompt
    assert [r.role for r in result.runs] == ["planner", "worker", "planner", "integrator"]
    assert "c.txt" in result.patch


def test_a_second_replan_round_can_fix_what_the_first_one_could_not(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("c"), plan_json("d"))
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    integrator = FakeHarness(None, name="integrator")
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator, replans=2), repo)

    assert len(planner.calls) == 3 and not integrator.calls
    subtasks = result.meta["subtasks"]
    assert [s["status"] for s in subtasks] == ["applied", "replanned", "replanned", "applied"]
    assert [(s["round"], s["worker"]) for s in subtasks] == [(0, "w1@m"), (0, "w2@m"), (1, "w2@m"), (2, "w1@m")]
    assert result.meta["replans"] == [
        {"round": 1, "failed": [2], "subtasks": [3]},
        {"round": 2, "failed": [3], "subtasks": [4]},
    ]
    assert "d.txt" in result.patch and "a.txt" in result.patch
    assert [r.role for r in result.runs] == ["planner", "worker", "planner", "planner", "worker"]


def test_replans_zero_never_asks_the_planner_again(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("b again"))

    async def integrate(call):
        await write(call.workspace, "b.txt", "integrator\n")
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator, replans=0), repo)

    assert len(planner.calls) == 1 and result.meta["replans"] == []
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "failed"]
    assert [r.role for r in result.runs] == ["planner", "worker", "integrator"]


def test_a_replan_planner_that_fails_is_recorded_and_the_integrator_takes_over(repo):
    planner, _ = planner_replying(plan_json("a", "b"), "no json here")

    async def integrate(call):
        await write(call.workspace, "b.txt", "integrator\n")
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator, replans=3), repo)

    (record,) = result.meta["replans"]  # a failed replan is not retried
    assert record["round"] == 1 and record["failed"] == [2] and record["subtasks"] == []
    assert "PlanParseError" in record["error"]
    planner_failure = next(f for f in result.meta["failures"] if f["role"] == "planner")
    assert planner_failure["round"] == 1 and planner_failure["error"] == record["error"]
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "failed"]
    assert [r.role for r in result.runs] == ["planner", "worker", "planner", "integrator"]
    assert "Subtask 2 (b): MISSING" in integrator.calls[0].prompt and "b.txt" in result.patch


def test_a_replan_planner_that_crashes_is_recorded_too(repo):
    async def script(call):
        if call.number == 2:
            raise RuntimeError("planner down")
        return plan_json("a", "b")

    async def integrate(call):
        return "done"

    integrator = FakeHarness(integrate, name="integrator")
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    result, _ = solve(PlanTeam(FakeHarness(script, name="planner"), [w1, w2], integrator), repo)
    assert "planner down" in result.meta["replans"][0]["error"]
    assert [r.role for r in result.runs] == ["planner", "worker", "integrator"]


def test_a_replan_with_no_new_subtasks_means_nothing_is_left(repo):
    planner, _ = planner_replying(plan_json("a", "b"), json.dumps({"subtasks": []}))
    integrator = FakeHarness(None, name="integrator")
    w1, w2 = titled_worker("w1"), titled_worker("w2", always_fail=True)
    result, _ = solve(PlanTeam(planner, [w1, w2], integrator), repo)

    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "replanned"]
    assert result.meta["replans"] == [{"round": 1, "failed": [2], "subtasks": []}]
    assert not integrator.calls and "a.txt" in result.patch and "b.txt" not in result.patch
    assert [r.role for r in result.runs] == ["planner", "worker", "planner"]


def test_a_worker_that_changed_nothing_is_replanned_with_that_reason(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("b again"))
    w1 = titled_worker("w1")

    async def lazy_then_working(call):
        if call.number == 1:
            return "nothing to do"
        await write(call.workspace, "b.txt", "w2\n")
        return "done"

    w2 = FakeHarness(lazy_then_working, name="w2")
    result, _ = solve(PlanTeam(planner, [w1, w2]), repo)
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "replanned", "applied"]
    assert "- 2. b: its worker produced no changes" in planner.calls[1].prompt
    assert "b.txt" in result.patch


def test_conflicts_are_not_replanned_the_integrator_handles_them(repo):
    planner, _ = planner_replying(plan_json("p", "q"))
    conflicting = [worker_writing({"a.txt": "one\n"}, "w1"), worker_writing({"a.txt": "two\n"}, "w2")]
    result, _ = solve(PlanTeam(planner, conflicting), repo)
    assert len(planner.calls) == 1 and result.meta["replans"] == []
    assert result.meta["integration"]["conflicts"] == [2] and result.meta["integration"]["integrator_runs"] == 1


def test_subtasks_of_a_replan_are_told_about_each_other_only(repo):
    planner, _ = planner_replying(plan_json("a", "b", "c"), plan_json("d", "e"))
    w1, w2 = titled_worker("w1", fail_calls=(1, 2)), titled_worker("w2", fail_calls=(1,))
    result, _ = solve(PlanTeam(planner, [w1, w2]), repo)
    # Round-1 workers see two subtasks: "(1 of 2)" and "(2 of 2)" with the other's title, nothing of round 0.
    prompts = [c.prompt for h in (w1, w2) for c in h.calls if "(1 of 2)" in c.prompt or "(2 of 2)" in c.prompt]
    assert len(prompts) == 2
    assert any("(1 of 2): d" in p and "2. e" in p and "1. a" not in p for p in prompts)
    assert any("(2 of 2): e" in p and "1. d" in p for p in prompts)
    assert [s["round"] for s in result.meta["subtasks"]] == [0, 0, 0, 1, 1]
    assert "d.txt" in result.patch and "e.txt" in result.patch


def test_the_cli_summary_shows_the_round_of_replanned_work(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("a again"))
    result, _ = solve(PlanTeam(planner, [titled_worker("w1", fail_calls=(1,)), titled_worker("w2")]), repo)
    out = StringIO()
    render_summary(
        Console(file=out, width=200, force_terminal=False),
        "plan",
        result,
        repo=str(repo),
        patch_path=repo / "final.patch",
        record_path=repo / "record.json",
    )
    lines = out.getvalue().splitlines()
    assert "subtask 1 [replanned]: a (w1@m)" in lines
    assert "subtask 2 [applied]: b (w2@m)" in lines
    assert "subtask 3 [applied] (round 1): a again (w2@m)" in lines


# --- per-subtask review -----------------------------------------------------------------------


def versioned_coder(name, path):
    """A worker whose n-th attempt writes ``<name> v<n>`` into ``path``."""

    async def script(call):
        await write(call.workspace, path, f"{name} v{call.number}\n")
        return "done"

    return FakeHarness(script, name=name)


def test_with_a_reviewer_every_subtask_is_a_review_loop_in_its_own_fork(repo):
    async def review(call):
        await write(call.workspace, "reviewer-scratch.txt", "x")  # a reviewer that ignores read-only
        return verdict_json(9, True) if " v2" in call.prompt else verdict_json(3, False, "needs work")

    reviewer = FakeHarness(review, name="reviewer")
    w1, w2 = versioned_coder("w1", "a.txt"), versioned_coder("w2", "b.txt")
    planner = planner_saying(plan_json("a", "b"))
    result, root = solve(PlanTeam(planner, [w1, w2], reviewer=reviewer, rounds=3), repo)

    assert "w1 v2" in result.patch and "w2 v2" in result.patch and "v1" not in result.patch
    assert "reviewer-scratch" not in result.patch and "planner-scratch" not in result.patch
    assert [(r.role, r.harness) for r in result.runs] == [("planner", "planner")] + [
        (role, name) for name in ("w1", "w2") for role, name in (("coder", name), ("reviewer", "reviewer")) * 2
    ]
    first, second = result.meta["subtasks"]
    assert first["review"] == {"scores": [3, 9], "accepted": True, "best_round": 2}
    assert second["review"] == {"scores": [3, 9], "accepted": True, "best_round": 2}
    assert (first["runs"], second["runs"]) == ([1, 2, 3, 4], [5, 6, 7, 8])
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "applied"]
    assert "needs work" in w1.calls[1].prompt  # the coder revises with the reviewer's feedback
    # The reviewer judged the subtask (not the whole task) in forks of the workers' forks.
    assert all("do a" in c.prompt or "do b" in c.prompt for c in reviewer.calls)
    forks = {*w1.roots, *w2.roots}
    assert len(forks) == 2 and forks.isdisjoint(reviewer.roots) and root not in reviewer.roots
    assert not any(os.path.exists(r) for r in [*forks, *reviewer.roots])
    assert result.meta["integration"]["integrator_runs"] == 0 and "failures" not in result.meta


def test_the_best_reviewed_patch_is_merged_not_the_last_attempt(repo):
    async def review(call):
        return verdict_json(9, False, "ok") if " v1" in call.prompt else verdict_json(2, False, "worse")

    reviewer = FakeHarness(review, name="reviewer")
    w1, w2 = versioned_coder("w1", "a.txt"), versioned_coder("w2", "b.txt")
    result, _ = solve(PlanTeam(planner_saying(plan_json("a", "b")), [w1, w2], reviewer=reviewer, rounds=2), repo)

    assert "w1 v1" in result.patch and "w2 v1" in result.patch and "v2" not in result.patch
    assert result.meta["subtasks"][0]["review"] == {"scores": [9, 2], "accepted": False, "best_round": 1}
    assert len(w1.calls) == 2  # the loop ran both rounds; only the first attempt was kept


def test_a_reviewer_that_fails_is_recorded_against_its_subtask_and_the_patch_is_kept(repo):
    async def review(call):
        raise RuntimeError("reviewer down")

    w1, w2 = versioned_coder("w1", "a.txt"), versioned_coder("w2", "b.txt")
    team = PlanTeam(
        planner_saying(plan_json("a", "b")), [w1, w2], reviewer=FakeHarness(review, name="reviewer"), rounds=1
    )
    result, _ = solve(team, repo)

    assert "w1 v1" in result.patch and "w2 v1" in result.patch
    assert [s["status"] for s in result.meta["subtasks"]] == ["applied", "applied"]
    failures = result.meta["failures"]
    # the review loop's own round is its attempt: in a plan, `round` is the replan round
    assert sorted((f["subtask"], f["role"], f["attempt"]) for f in failures) == [(1, "reviewer", 1), (2, "reviewer", 1)]
    assert all("reviewer down" in f["error"] and "round" not in f for f in failures)


def test_a_review_loop_that_crashes_after_paid_runs_still_reports_them(repo):
    async def break_the_fork(call):
        await write(call.workspace, "a.txt", "a\n")
        os.remove(os.path.join(call.workspace.root, ".git"))  # the loop's diff of this fork now raises
        return "done"

    coder = FakeHarness(break_the_fork, name="w1")
    reviewer = FakeHarness(None, name="reviewer")
    team = PlanTeam(
        planner_saying(plan_json("a", "b")),
        [coder, worker_writing({"b.txt": "b\n"}, "w2")],
        integrator=worker_writing({}, "integrator"),
        replans=0,
        reviewer=reviewer,
        rounds=1,
    )
    result, _ = solve(team, repo)

    assert [(r.role, r.harness) for r in result.runs][:2] == [("planner", "planner"), ("coder", "w1")]
    first = result.meta["subtasks"][0]
    assert first["status"] == "failed" and first["runs"] == [1]
    assert any(f.get("subtask") == 1 and "Error" in f["error"] for f in result.meta["failures"])


def test_a_reviewed_subtask_that_changes_nothing_is_replanned(repo):
    planner, _ = planner_replying(plan_json("a", "b"), plan_json("a again"))

    async def approve(call):
        return verdict_json(9, True)

    async def do_nothing(call):
        return "nothing to do"

    lazy = FakeHarness(do_nothing, name="lazy")
    w2 = titled_worker("w2")
    result, _ = solve(PlanTeam(planner, [lazy, w2], reviewer=FakeHarness(approve, name="reviewer")), repo)

    subtasks = result.meta["subtasks"]
    assert [s["status"] for s in subtasks] == ["replanned", "applied", "applied"]
    assert subtasks[0]["review"]["scores"][0] == 0  # an empty patch scores 0 without a reviewer run
    assert [s["round"] for s in subtasks] == [0, 0, 1] and "a again.txt" in result.patch
    assert "no changes" in planner.calls[1].prompt


@pytest.mark.parametrize(
    "plan,reason",
    [("no json here", "did not return a valid plan"), (plan_json("only"), "single subtask")],
)
def test_the_solo_fallback_becomes_a_review_loop_when_there_is_a_reviewer(repo, plan, reason):
    async def approve(call):
        return verdict_json(9, True)

    reviewer = FakeHarness(approve, name="reviewer")
    w1, w2 = versioned_coder("w1", "all.txt"), versioned_coder("w2", "never.txt")
    result, _ = solve(PlanTeam(planner_saying(plan), [w1, w2], reviewer=reviewer), repo)

    assert "w1 v1" in result.patch and not w2.calls
    assert [r.role for r in result.runs] == ["planner", "coder", "reviewer"]
    fallback = result.meta["fallback"]
    assert fallback["to"] == "review" and fallback["worker"] == "w1@m" and reason in fallback["reason"]
    assert fallback["reviewer"] == "reviewer@m"
    assert result.meta["review"] == {"scores": [9], "accepted": True, "best_round": 1}
    assert TASK.instruction in reviewer.calls[0].prompt  # the whole task is what gets reviewed


def test_a_plan_team_without_a_reviewer_has_no_review_meta(repo):
    result, _ = solve(PlanTeam(planner_saying("no json"), [worker_writing({"a.txt": "a\n"}, "w1")]), repo)
    assert result.meta["fallback"]["to"] == "solo" and "review" not in result.meta
    assert "review" not in PlanTeam(planner_saying(plan_json("a", "b")), [worker_writing({}, "w")]).name


# --- options ----------------------------------------------------------------------------------


def test_plan_team_names_spell_out_replans_and_the_reviewer():
    planner, worker, reviewer = FakeHarness(None, name="p"), FakeHarness(None, name="w"), FakeHarness(None, name="r")
    assert PlanTeam(planner, [worker]).name == "plan:planner=p@m,workers=w@m,integrator=w@m,max_tasks=4,replans=1"
    team = PlanTeam(planner, [worker], replans=0, reviewer=reviewer, rounds=2)
    assert team.name == "plan:planner=p@m,workers=w@m,integrator=w@m,max_tasks=4,replans=0,reviewer=r@m,rounds=2"


@pytest.mark.parametrize(
    "options",
    [{"replans": -1}, {"rounds": 0}, {"reviewer": CheckHarness("true")}],
)
def test_invalid_plan_team_options_are_rejected(options):
    with pytest.raises(ValueError):
        PlanTeam(FakeHarness(None, name="p"), [FakeHarness(None, name="w")], **options)


# --- spec -------------------------------------------------------------------------------------


def test_plan_spec_has_a_canonical_name_that_spells_out_the_defaults():
    team = build_team("plan:planner=claude@haiku,workers=claude@haiku+codex@gpt-6-luna")
    assert team.name == (
        "plan:planner=claude@haiku,workers=claude@haiku+codex@gpt-6-luna,integrator=claude@haiku,max_tasks=4,"
        "replans=1"
    )
    assert build_team(team.name).name == team.name
    assert isinstance(team, PlanTeam) and len(team.workers) == 2 and team.max_tasks == 4
    assert team.integrator.name == "claude" and team.integrator.model == "haiku"


def test_plan_spec_accepts_a_single_worker_integrator_and_max_tasks():
    team = build_team("plan:max_tasks=2,workers=codex,planner=claude,integrator=sdk@gpt-5-mini")
    assert team.name == "plan:planner=claude,workers=codex,integrator=sdk@gpt-5-mini,max_tasks=2,replans=1"
    assert team.max_tasks == 2 and team.integrator.name == "sdk"
    spec = parse_team_spec(team.name)
    assert [str(h) for h in spec.harness_specs] == ["claude", "sdk@gpt-5-mini", "codex"]


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ("plan:workers=claude", "planner"),
        ("plan:planner=claude", "workers"),
        ("plan:planner=claude,workers=claude,max_tasks=0", "max_tasks"),
        ("plan:planner=claude,workers=claude,max_tasks=x", "max_tasks"),
        ("plan:planner=claude,workers=claude,rounds=2", "only valid together with reviewer"),
        ("plan:planner=claude,workers=claude,replans=-1", "replans"),
        ("plan:planner=claude,workers=claude,replans=x", "replans"),
        ("plan:planner=claude,workers=claude,reviewer=check", "plan team"),
        ("plan:planner=claude,workers=claude,reviewer=claude,rounds=0", "rounds"),
        ("plan:planner=gemini,workers=claude", "unknown harness"),
        ("plan:planner=check,workers=claude", "planner"),
        ("plan:planner=claude,workers=claude+check", "worker"),
        ("plan:planner=claude,workers=claude,integrator=check", "integrator"),
        ("plan:planner=claude,workers=claude++codex", "unknown harness"),
    ],
)
def test_invalid_plan_specs_are_rejected_with_a_helpful_message(spec, fragment):
    with pytest.raises(SpecError, match=fragment):
        build_team(spec, check="true")


def test_after_merging_the_workspace_looks_like_any_other_topologys_nothing_staged(repo):
    seen = {}

    async def integrate(call):
        seen["staged"] = (await call.workspace.exec(["git", "diff", "--cached", "--stat"])).stdout
        seen["diff"] = (await call.workspace.exec(["git", "diff", "HEAD", "--stat"])).stdout
        seen["untracked"] = (await call.workspace.exec(["git", "ls-files", "--others", "--exclude-standard"])).stdout
        await write(call.workspace, "a.txt", "merged\n")
        return "merged"

    w1 = worker_writing({"a.txt": "one\n", "x.txt": "x\n"}, "w1")
    w2 = worker_writing({"a.txt": "two\n"}, "w2")
    solve(PlanTeam(planner_saying(plan_json("p", "q")), [w1, w2], FakeHarness(integrate, name="i")), repo)
    assert seen["staged"].strip() == ""
    assert "a.txt" in seen["diff"] and "x.txt" in seen["untracked"]


def _git_result(handler):
    return ScriptedWorkspace(lambda argv, stdin: handler(argv[1]) or ExecResult(0, "tree123\n", ""))


def test_a_failed_snapshot_is_reported_without_touching_the_workspace():
    ws = _git_result(lambda cmd: ExecResult(1, "", "no index") if cmd == "write-tree" else None)
    error, clean = run(_apply_3way(ws, "patch"))
    assert "could not snapshot" in error and "no index" in error and clean is True
    assert [c[1] for c in ws.calls] == ["add", "write-tree"]  # never attempted the apply


def test_a_failed_rollback_is_reported_and_marks_the_workspace_unclean():
    def handler(cmd):
        if cmd == "apply":
            return ExecResult(1, "", "conflict in a.txt")
        if cmd == "read-tree":
            return ExecResult(128, "", "cannot reset")

    ws = _git_result(handler)
    error, clean = run(_apply_3way(ws, "patch"))
    assert "conflict in a.txt" in error and "rollback failed" in error and "cannot reset" in error
    assert clean is False


def test_a_successful_apply_needs_no_rollback():
    ws = _git_result(lambda cmd: None)
    assert run(_apply_3way(ws, "patch")) == (None, True)
    assert [c[1] for c in ws.calls] == ["add", "write-tree", "apply"]


def test_max_concurrency_bounds_how_many_workers_run_at_once(repo):
    state = {"now": 0, "peak": 0}

    async def script(call):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.3)
        number = call.prompt.split("Your subtask (")[1].split(" of")[0]
        await write(call.workspace, f"s{number}.txt", "x\n")
        state["now"] -= 1
        return "done"

    worker = FakeHarness(script, name="w")
    team = PlanTeam(planner_saying(plan_json("a", "b", "c")), [worker], max_concurrency=2)
    result, _ = solve(team, repo)
    assert state["peak"] == 2 and all(f"s{n}.txt" in result.patch for n in (1, 2, 3))
    with pytest.raises(ValueError):
        PlanTeam(planner_saying(plan_json("a")), [worker], max_concurrency=0)


def test_the_integrator_prompt_stays_within_the_patch_budget_however_many_subtasks():
    worker = FakeHarness(None)
    states = [
        _SubtaskState(i, Subtask(f"t{i}", "do"), worker, patch="+x" * 20_000, status="conflict")
        for i in range(30)
    ]
    prompt = PlanTeam._integrate_prompt(TASK, states)
    assert len(prompt) < MAX_PATCH_IN_PROMPT + 10_000

import asyncio
from dataclasses import replace

from agent.bench.check import SUITE_CHECK, SuiteChecks, compare
from agent.bench.dataset import BenchTask, GradingSpec
from agent.bench.sandbox import ModalBackend, SandboxConfig
from agent.teams import CodingTask, SoloTeam
from agent.teams.harnesses.check import CheckRunner
from agent.teams.types import TeamResult

from .fakes import FakeModal
from .repos import FIXED, make_patch, make_template

# The repository's visible suite (pytest-style lines): one test always passes, one follows
# notes.txt, one already fails at the base commit, a test the agent adds fails when present,
# nothing runs at all if calc.py "does not build", and a test only the hidden test patch adds.
SUITE = (
    "grep -q BROKEN calc.py && exit 0; "
    'echo "PASSED calc::test_keep"; '
    'if grep -q "keep me" notes.txt; then echo "PASSED notes::test_notes"; '
    'else echo "FAILED notes::test_notes"; fi; '
    'echo "FAILED old::test_already_broken"; '
    '[ -f added_test.txt ] && echo "FAILED new::test_added"; '
    '[ -f hidden.txt ] && echo "FAILED hidden::test_leak"; true'
)


class Env:
    def __init__(self, tmp_path, suite=SUITE):
        self.template, base, _ = make_template(tmp_path)
        (tmp_path / "work").mkdir()
        self.modal = FakeModal(self.template, tmp_path / "work")
        self.backend = ModalBackend(SandboxConfig(), modal=self.modal)
        self.task = BenchTask(
            task_id="owner__repo-1", instruction="fix it", repo="owner/repo", base_commit=base,
            image="starryzhang/sweb.eval.x86_64.owner_1776_repo-1", language="python", dataset="test",
            grading=GradingSpec(
                test_patch=make_patch(self.template, {"hidden.txt": "hidden test\n"}),
                fail_to_pass=("calc::test_add",),
                pass_to_pass=("calc::test_keep",),
                test_cmds=(suite,),
                log_parser="pytest",
            ),
        )
        self.checks = SuiteChecks(self.backend)

    def patch(self, files):
        return make_patch(self.template, files)

    def check(self, *patches):
        runner = CheckRunner(self.checks.for_task(self.task), workspace=None)

        async def go():
            return [await runner(p) for p in patches]

        return asyncio.run(go())


def test_compare_ignores_tests_that_already_failed_and_counts_vanished_ones():
    base = {"a": "pass", "b": "fail", "c": "pass", "d": "skip"}
    now = {"a": "fail", "b": "fail", "e": "fail", "f": "pass", "d": "fail"}
    assert compare(base, now) == (["a"], ["c"], ["e"])


def test_a_fix_that_breaks_nothing_passes_and_the_base_commit_runs_once(tmp_path):
    env = Env(tmp_path)
    good, other = env.check(env.patch({"calc.py": FIXED}), env.patch({"calc.py": FIXED, "x.txt": "x\n"}))

    assert good.passed and good.exit_code == 0 and good.command == SUITE_CHECK
    assert "3 tests ran: 2 passed, 1 failed, 0 skipped" in good.output_tail
    assert "0 newly failing, 0 no longer run, 0 new tests failing" in good.output_tail
    assert other.passed
    # Base commit once plus one sandbox per patch; all clean grading sandboxes, all terminated.
    assert len(env.modal.sandboxes) == 3 and all(sb.terminated for sb in env.modal.sandboxes)
    assert all(c["image"].commands == [] and not c.get("secrets") for c in env.modal.created)


def test_the_hidden_tests_are_never_applied(tmp_path):
    env = Env(tmp_path)
    (result,) = env.check(env.patch({"calc.py": FIXED}))
    assert result.passed and "hidden" not in result.output_tail


def test_a_test_that_passed_at_the_base_commit_and_now_fails_is_named(tmp_path):
    env = Env(tmp_path)
    (result,) = env.check(env.patch({"notes.txt": "changed\n"}))
    assert not result.passed and result.exit_code == 1
    assert "Passed at the base commit, now fail: notes::test_notes" in result.output_tail
    assert "old::test_already_broken" not in result.output_tail.split("Log (tail):")[0]


def test_a_patch_that_breaks_the_build_fails_because_its_tests_no_longer_run(tmp_path):
    env = Env(tmp_path)
    (result,) = env.check(env.patch({"calc.py": "BROKEN\n"}))
    assert not result.passed
    assert "no longer run (does it still build?): calc::test_keep, notes::test_notes" in result.output_tail


def test_a_new_test_that_fails_fails_the_check(tmp_path):
    env = Env(tmp_path)
    (result,) = env.check(env.patch({"added_test.txt": "x\n"}))
    assert not result.passed and "New tests that fail: new::test_added" in result.output_tail


def test_a_patch_that_does_not_apply_fails(tmp_path):
    env = Env(tmp_path)
    garbage = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-nope\n+x\n"
    (result,) = env.check(garbage)
    assert not result.passed and "did not apply" in result.output_tail


def test_a_suite_with_no_results_at_the_base_commit_cannot_judge(tmp_path):
    env = Env(tmp_path, suite="echo nothing here")
    (result,) = env.check(env.patch({"calc.py": FIXED}))
    assert not result.passed and result.exit_code is None
    assert "could not run" in result.output_tail and "no test results" in result.output_tail


def test_a_timed_out_suite_is_a_failed_check(tmp_path):
    env = Env(tmp_path, suite=SUITE)
    env.checks.timeout_s = 0.5
    slow_when_fixed = 'grep -q "a + b" calc.py && sleep 5; ' + SUITE
    env.task = replace(env.task, grading=replace(env.task.grading, test_cmds=(slow_when_fixed,)))
    (result,) = env.check(env.patch({"calc.py": FIXED}))
    assert not result.passed and result.exit_code is None and "timed out" in result.output_tail


def test_teams_sharing_a_task_share_its_base_commit_run(tmp_path):
    env = Env(tmp_path)
    fix = env.patch({"calc.py": FIXED})

    async def go():
        a = CheckRunner(env.checks.for_task(env.task), workspace=None)
        b = CheckRunner(env.checks.for_task(env.task), workspace=None)
        return await asyncio.gather(a(fix), b(fix))

    first, second = asyncio.run(go())
    assert first.passed and second.passed
    assert len(env.modal.sandboxes) == 3  # one base-commit run, one per team's patch


def test_the_solo_team_records_the_suite_check(tmp_path):
    env = Env(tmp_path)
    fix = env.patch({"calc.py": FIXED})

    class Workspace:
        async def diff(self):
            return fix

    class Worker:
        name, model, supports_output_schema = "fake", None, False

        async def run(self, workspace, prompt, **kwargs):
            from agent.teams.types import HarnessRun, Usage

            return HarnessRun("fake", None, "worker", "done", 0, Usage(cost_usd=0.0), 0.1)

    team = SoloTeam(Worker(), check=env.checks.for_task(env.task))
    result = asyncio.run(team.solve(CodingTask("t", "fix it"), Workspace()))
    assert isinstance(result, TeamResult) and result.meta["check"]["passed"] is True

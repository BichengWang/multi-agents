import asyncio
from pathlib import Path

import pytest

from agent.bench.dataset import BenchTask, GradingSpec, load_tasks
from agent.bench.evaluate import GradingError, evaluate_patch
from agent.bench.sandbox import ModalBackend, SandboxConfig

from .fakes import FakeModal
from .repos import CHECK_RS_SH, CHECK_SH, FIXED, make_patch, make_template

FIXTURE = Path(__file__).parent / "fixtures" / "rows.jsonl"


def rust_parser():
    task = next(t for t in load_tasks(str(FIXTURE)) if t.task_id == "jj-vcs__jj-6728")
    return task.grading.log_parser


class Env:
    def __init__(self, tmp_path, spec_kind="python"):
        self.template, self.base, _ = make_template(tmp_path)
        (tmp_path / "work").mkdir()
        self.modal = FakeModal(self.template, tmp_path / "work")
        self.backend = ModalBackend(SandboxConfig(), modal=self.modal)
        if spec_kind == "python":
            test_patch = make_patch(self.template, {"check.sh": CHECK_SH})
            spec = GradingSpec(
                test_patch=test_patch,
                fail_to_pass=("calc::test_add",),
                pass_to_pass=("calc::test_keep",),
                test_cmds=("bash check.sh",),
                log_parser="pytest",
            )
        else:
            test_patch = make_patch(self.template, {"check_rs.sh": CHECK_RS_SH})
            spec = GradingSpec(
                test_patch=test_patch,
                fail_to_pass=("m::t_add",),
                pass_to_pass=("m::t_keep",),
                rebuild_cmds=("touch rebuilt.marker",),
                test_cmds=("sh check_rs.sh > out.log",),
                print_cmds=("cat out.log",),
                log_parser=rust_parser(),
            )
        self.task = BenchTask(
            task_id="owner__repo-1", instruction="fix it", repo="owner/repo",
            base_commit=self.base, image="starryzhang/sweb.eval.x86_64.owner_1776_repo-1",
            language="python", dataset="test", grading=spec,
        )
        self.gold = make_patch(self.template, {"calc.py": FIXED})

    def evaluate(self, patch, **kwargs):
        return asyncio.run(evaluate_patch(self.task, patch, backend=self.backend, **kwargs))


@pytest.mark.parametrize("kind", ["python", "multilang"])
def test_gold_patch_resolves_and_wrong_patches_do_not(tmp_path, kind):
    env = Env(tmp_path, kind)
    good = env.evaluate(env.gold)
    assert good.resolved, good.log
    assert good.test_patch_applied and good.patch_applied
    assert good.report.fail_to_pass_success and good.report.pass_to_pass_success
    first = env.modal.created[0]
    assert first["image"].tag == env.task.image  # the instance's own image
    assert first["image"].commands == [] and not first.get("secrets")  # no CLIs, no API keys

    wrong = env.evaluate(make_patch(env.template, {"calc.py": "def add(a, b):\n    return a * b\n"}))
    assert not wrong.resolved and wrong.patch_applied
    assert wrong.report.fail_to_pass_failure and wrong.report.pass_to_pass_failure == []
    assert wrong.to_json()["report"]["FAIL_TO_PASS"]["failure"]

    untouched = env.evaluate(make_patch(env.template, {"notes.txt": "changed\n"}))
    assert not untouched.resolved  # the bug is still there
    # Every grading sandbox is its own fresh instance image, and none is left running.
    assert len(env.modal.sandboxes) == 3
    assert all(sb.terminated for sb in env.modal.sandboxes)


def test_empty_patch_is_never_graded(tmp_path):
    env = Env(tmp_path)
    result = env.evaluate("  \n")
    assert not result.resolved and result.empty_patch
    assert env.modal.created == []  # no sandbox was even started


def test_patch_that_does_not_apply_is_reported_like_upstream(tmp_path):
    env = Env(tmp_path)
    garbage = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-nope\n+x\n"
    result = env.evaluate(garbage)
    assert not result.resolved and result.patch_applied is False and result.test_patch_applied
    assert result.report.fail_to_pass_failure == ["calc::test_add"]


def test_tests_that_hang_are_unresolved_not_errors(tmp_path):
    env = Env(tmp_path)
    env.task = BenchTask(
        **{**vars(env.task), "grading": GradingSpec(
            test_patch=env.task.grading.test_patch, fail_to_pass=("a",), pass_to_pass=(),
            test_cmds=("sleep 30",), log_parser="pytest",
        )}
    )
    result = env.evaluate(env.gold, timeout_s=0.2)
    assert result.timed_out and not result.resolved
    assert all(sb.terminated for sb in env.modal.sandboxes)


def test_unparseable_logs_are_grading_errors(tmp_path):
    env = Env(tmp_path)
    env.task = BenchTask(
        **{**vars(env.task), "grading": GradingSpec(
            test_patch=env.task.grading.test_patch, fail_to_pass=("a",), pass_to_pass=(),
            test_cmds=("bash check.sh",), log_parser="def parser(log):\n    raise RuntimeError('x')\n",
        )}
    )
    with pytest.raises(GradingError):
        env.evaluate(env.gold)
    assert all(sb.terminated for sb in env.modal.sandboxes)


def test_real_fixture_instance_carries_gold_patch_and_commands():
    task = next(t for t in load_tasks(str(FIXTURE)) if t.task_id == "jj-vcs__jj-6728")
    assert task.grading.gold_patch.startswith("diff --git")
    assert task.grading.rebuild_cmds and task.grading.print_cmds

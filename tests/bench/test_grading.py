import json
from pathlib import Path

import pytest

from agent.bench.dataset import GradingSpec, load_tasks
from agent.bench.evaluate import (
    APPLY_MARK,
    LOG_BEGIN,
    GradingError,
    build_eval_script,
    default_pytest_parser,
    grade,
    parse_log_pytest,
    run_log_parser,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rows.jsonl"

# `pytest -rA` short test summary, the format SWE-bench-Live's Python instances log.
PYTEST_LOG = """\
============================= test session starts ==============================
collected 7 items

tests/test_a.py ....F.                                                   [ 85%]
=========================== short test summary info ============================
PASSED tests/test_a.py::test_one
PASSED tests/test_a.py::test_two[param-1]
XFAIL tests/test_a.py::test_expected_failure
SKIPPED [1] tests/test_a.py:30: needs network
SKIPPED tests/test_a.py::test_skipped
ERROR tests/test_b.py::test_broken_fixture - RuntimeError: nope
FAILED tests/test_a.py::test_regression - AssertionError: assert 1 == 2
========================= 1 failed, 2 passed in 0.12s ==========================
"""


def test_pytest_parser_normalises_statuses_like_upstream():
    raw = parse_log_pytest(PYTEST_LOG)
    assert raw["tests/test_a.py::test_one"] == "PASSED"
    assert raw["tests/test_a.py::test_regression"] == "FAILED"  # " - message" stripped first
    assert "[1]" in raw  # upstream quirk: "SKIPPED [1] ..." keys on the second token
    status = default_pytest_parser(PYTEST_LOG)
    assert status["tests/test_a.py::test_one"] == "pass"
    assert status["tests/test_a.py::test_two[param-1]"] == "pass"
    assert status["tests/test_a.py::test_expected_failure"] == "pass"  # XFAIL stays "expected"
    assert status["tests/test_a.py::test_skipped"] == "skip"
    assert status["tests/test_b.py::test_broken_fixture"] == "fail"  # ERROR counts as failure
    assert status["tests/test_a.py::test_regression"] == "fail"


def test_grade_follows_the_upstream_verdict():
    status = {"f1": "pass", "f2": "pass", "p1": "pass", "p2": "skip", "other": "fail"}
    ok = grade(status, ("f1", "f2"), ("p1", "p2"))
    assert ok.resolved
    assert ok.fail_to_pass_success == ["f1", "f2"] and ok.pass_to_pass_success == ["p1"]
    assert ok.pass_to_pass_missing == []  # p2 skipped is *seen*, just neither pass nor fail

    broke_p2p = grade({**status, "p1": "fail"}, ("f1", "f2"), ("p1", "p2"))
    assert not broke_p2p.resolved and broke_p2p.pass_to_pass_failure == ["p1"]

    f2p_fails = grade({**status, "f2": "fail"}, ("f1", "f2"), ("p1",))
    assert not f2p_fails.resolved and f2p_fails.fail_to_pass_failure == ["f2"]

    # A fail-to-pass test that never shows up in the log is not a pass.
    f2p_missing = grade({"f1": "pass", "p1": "pass"}, ("f1", "f2"), ("p1",))
    assert not f2p_missing.resolved and f2p_missing.fail_to_pass_missing == ["f2"]

    # Upstream quirk, kept for comparability and surfaced separately: a vanished pass-to-pass
    # test does not fail the instance.
    p2p_missing = grade({"f1": "pass", "f2": "pass"}, ("f1", "f2"), ("p1",))
    assert p2p_missing.resolved and p2p_missing.pass_to_pass_missing == ["p1"]

    payload = ok.to_json()
    assert payload["resolved"] is True and payload["FAIL_TO_PASS"]["success"] == ["f1", "f2"]


def fixture_task(task_id):
    return next(t for t in load_tasks(str(FIXTURE)) if t.task_id == task_id)


def test_dataset_parser_script_grades_a_recorded_rust_libtest_log():
    task = fixture_task("jj-vcs__jj-6728")
    f2p, p2p = task.grading.fail_to_pass, task.grading.pass_to_pass
    log = "\n".join(
        [
            "running 40 tests",
            *[f"test {name} ... ok" for name in f2p],
            "test other::tests::ignored_one ... ignored",
            "test result: ok. 40 passed; 0 failed; 1 ignored",
        ]
    )
    status = run_log_parser(task.grading.log_parser, log)
    assert all(status[name] == "pass" for name in f2p)
    assert status["other::tests::ignored_one"] == "skip"
    assert grade(status, f2p, p2p).resolved

    failing = log.replace(f"test {f2p[0]} ... ok", f"test {f2p[0]} ... FAILED")
    failing += f"\n---- {f2p[0]} stdout ----\nthread panicked"
    status = run_log_parser(task.grading.log_parser, failing)
    assert status[f2p[0]] == "fail"
    assert not grade(status, f2p, p2p).resolved


def test_dataset_parser_script_grades_a_recorded_go_test_json_log():
    task = fixture_task("oauth2-proxy__oauth2-proxy-2524")
    target = task.grading.fail_to_pass[0]
    other = task.grading.pass_to_pass[0]
    log = "\n".join(
        json.dumps(e)
        for e in (
            {"Action": "run", "Package": "p", "Test": target},
            {"Action": "output", "Package": "p", "Test": target, "Output": f"--- PASS: {target}\n"},
            {"Action": "pass", "Package": "p", "Test": target},
            {"Action": "fail", "Package": "p", "Test": other},
        )
    )
    status = run_log_parser(task.grading.log_parser, log)
    assert status == {target: "pass", other: "fail"}
    report = grade(status, task.grading.fail_to_pass[:1], task.grading.pass_to_pass[:1])
    assert not report.resolved and report.pass_to_pass_failure == [other]


def test_pytest_keyword_selects_the_builtin_parser():
    assert run_log_parser("pytest", PYTEST_LOG) == default_pytest_parser(PYTEST_LOG)
    assert run_log_parser(" PyTest ", "PASSED a::b") == {"a::b": "pass"}


def test_parser_scripts_run_isolated_without_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    script = (
        "def parser(log):\n"
        "    import os\n"
        "    print('noise that must not corrupt the result')\n"
        "    return {'key=' + os.environ.get('ANTHROPIC_API_KEY', 'absent'): 'pass'}\n"
    )
    assert run_log_parser(script, "") == {"key=absent": "pass"}


def test_broken_parsers_are_grading_errors_not_unresolved():
    with pytest.raises(GradingError):
        run_log_parser("def parser(log):\n    raise ValueError('bad log')\n", "x")
    with pytest.raises(GradingError):
        run_log_parser("x = 1\n", "x")
    with pytest.raises(GradingError):
        run_log_parser("def parser(log):\n    while True: pass\n", "x", timeout=0.5)


def test_eval_script_for_multilang_instances_keeps_one_shell_session_in_upstream_order():
    task = fixture_task("github__gh-aw-27242")
    script = build_eval_script(task.grading, test_patch_path="/tmp/t.diff", patch_path="/tmp/p.diff")
    lines = script.splitlines()
    def where(predicate):
        return next(i for i, line in enumerate(lines) if predicate(line))

    order = [
        where(lambda line: line.startswith("cd /testbed")),
        where(lambda line: "git apply --reject --whitespace=nowarn /tmp/t.diff" in line),
        where(lambda line: "git apply --reject --whitespace=nowarn /tmp/p.diff" in line),
        where(lambda line: line == " ; ".join(task.grading.rebuild_cmds)),
        where(lambda line: line == " ; ".join(task.grading.test_cmds)),
        lines.index(f'echo "{LOG_BEGIN}"'),
        lines.index(" ; ".join(task.grading.print_cmds)),
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
    assert f'echo "{APPLY_MARK} test_patch exit=$?"' in script
    assert "/tmp/t.diff" in lines[order[1]] and "/tmp/p.diff" in lines[order[2]]


def test_eval_script_for_python_instances_wraps_the_test_command_like_upstream():
    spec = GradingSpec(
        test_patch="",
        fail_to_pass=("a",),
        pass_to_pass=(),
        test_cmds=("pip install -e .", "pytest -rA"),
    )
    script = build_eval_script(spec, test_patch_path="/tmp/t.diff", patch_path="/tmp/p.diff")
    assert "cat > run_test.sh <<'BENCH_TEST_CMD'\npip install -e . ; pytest -rA\nBENCH_TEST_CMD" in script
    assert "bash run_test.sh > testlog.out 2>&1" in script
    assert script.rstrip().endswith("cat testlog.out")
    assert "rebuild" not in script

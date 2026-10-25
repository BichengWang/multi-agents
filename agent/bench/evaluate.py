"""Hidden-test grading with SWE-bench-Live's official logic.

Ported (not re-invented) from ``evaluation/evaluation.py`` in microsoft/SWE-bench-Live at commit
``62d02936a0726fbf4597b05bd07b09fb99eddae6`` and the ``apply_patch`` / ``run_parser`` helpers of
microsoft/RepoLaunch at ``099857f37e87e7bf09a2baa1c78809518c485c53`` (both MIT). The official script
drives a Docker daemon through RepoLaunch; here the container steps run through the sandbox backend
(a Modal sandbox or a local Docker container started from the same instance image), and the logic
that grades test results is the same, with patch application checked separately:

1. apply ``test_patch``, then the candidate patch, with ``git apply --reject --whitespace=nowarn``
   (a failed apply does not stop the diagnostic run, as upstream);
2. run ``rebuild_cmds`` (if any), then ``test_cmds``, joined with `` ; `` in one shell session;
3. take the output of ``print_cmds`` as the test log (for the Python dataset, which has none, the
   tests run as ``bash run_test.sh > testlog.out 2>&1`` and the log is ``cat testlog.out``);
4. parse the log into ``{test: pass|fail|skip}`` with the instance's ``log_parser`` script (or the
   built-in pytest parser for ``pytest``);
5. resolved iff both patches applied fully, no ``PASS_TO_PASS`` test failed, no ``FAIL_TO_PASS``
   test failed, and every ``FAIL_TO_PASS`` test passed. Hidden-test-patch failure or missing apply
   status is a grading error; candidate-patch failure is unresolved even if the tests pass.

Hidden fields (``test_patch``, ``FAIL_TO_PASS``, ``PASS_TO_PASS``, commands) exist only in the
grading sandbox and in this process; an agent never sees them. Dataset parser scripts are executed
in an isolated Python subprocess with an empty environment, not in the grading sandbox (not every
instance image has Python) and never with API keys in reach.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .dataset import BenchTask, GradingSpec
from .sandbox import SandboxBackend, SandboxExecTimeout, SandboxWorkspace

# Upstream per-command timeout is 150 minutes.
TEST_TIMEOUT_S = 150 * 60

LOG_BEGIN = "<<<BENCH_TEST_LOG_BEGIN>>>"
APPLY_MARK = "[bench-apply]"

PARSER_TIMEOUT_S = 120

# ``python -I``: isolated mode; parser scripts see only the standard library.
_PARSER_RUNNER = r"""
import io, json, sys
payload = json.load(sys.stdin)
namespace = {}
real_stdout, real_stderr = sys.stdout, sys.stderr
sys.stdout = sys.stderr = io.StringIO()
try:
    exec(payload["script"], namespace)
    if "parser" not in namespace:
        raise NotImplementedError("Script must define 'parser' function")
    result = namespace["parser"](payload["log"]) or {}
finally:
    sys.stdout, sys.stderr = real_stdout, real_stderr
json.dump({str(k): str(v) for k, v in result.items()}, sys.stdout)
"""


class GradingError(RuntimeError):
    """Grading setup, parsing or sandbox failure; the pair is errored, not unresolved."""


class _TestStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


def parse_log_pytest(log: str) -> dict[str, str]:
    """Upstream ``parse_log_pytest`` (copied from SWE-bench's constants)."""
    status: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(x.value) for x in _TestStatus):
            if line.startswith(_TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) <= 1:
                continue
            status[parts[1]] = parts[0]
    return status


def default_pytest_parser(log: str) -> dict[str, str]:
    """Upstream ``default_pytest_parser``: normalise to ``pass`` / ``skip`` / ``fail``."""
    mapping = parse_log_pytest(log)
    for test, status in list(mapping.items()):
        if status.upper() == "XFAIL":
            mapping[test] = "pass"
        elif "pass" in status.lower():
            mapping[test] = "pass"
        elif "skip" in status.lower():
            mapping[test] = "skip"
        else:
            mapping[test] = "fail"
    return mapping


def run_log_parser(parser: str, log: str, *, timeout: float = PARSER_TIMEOUT_S) -> dict[str, str]:
    if parser.lower().strip() == "pytest":
        return default_pytest_parser(log)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PARSER_RUNNER],
            input=json.dumps({"script": parser, "log": log}).encode("utf-8", "surrogateescape"),
            capture_output=True,
            timeout=timeout,
            env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise GradingError(f"log parser timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise GradingError(
            "log parser failed: " + proc.stderr.decode("utf-8", "replace").strip()[-1500:]
        )
    return json.loads(proc.stdout)


@dataclass
class GradeReport:
    resolved: bool
    fail_to_pass_success: list[str] = field(default_factory=list)
    fail_to_pass_failure: list[str] = field(default_factory=list)
    pass_to_pass_success: list[str] = field(default_factory=list)
    pass_to_pass_failure: list[str] = field(default_factory=list)
    # Not part of upstream's verdict: expected tests that never appeared in the parsed log.
    fail_to_pass_missing: list[str] = field(default_factory=list)
    pass_to_pass_missing: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "FAIL_TO_PASS": {
                "success": self.fail_to_pass_success,
                "failure": self.fail_to_pass_failure,
                "missing": self.fail_to_pass_missing,
            },
            "PASS_TO_PASS": {
                "success": self.pass_to_pass_success,
                "failure": self.pass_to_pass_failure,
                "missing": self.pass_to_pass_missing,
            },
        }


def grade(
    status: dict[str, str], fail_to_pass: tuple[str, ...], pass_to_pass: tuple[str, ...]
) -> GradeReport:
    """Upstream ``run_instance`` verdict over a parsed ``{test: status}`` map."""
    passed = {t for t, s in status.items() if "pass" in s.lower()}
    failed = {t for t, s in status.items() if "fail" in s.lower()}
    f2p, p2p = set(fail_to_pass), set(pass_to_pass)
    f2p_ok, f2p_bad = f2p & passed, f2p & failed
    p2p_ok, p2p_bad = p2p & passed, p2p & failed
    all_f2p_pass = f2p <= f2p_ok or len(f2p_ok) == len(f2p)
    resolved = not p2p_bad and not f2p_bad and all_f2p_pass
    seen = set(status)
    return GradeReport(
        resolved=resolved,
        fail_to_pass_success=sorted(f2p_ok),
        fail_to_pass_failure=sorted(f2p_bad),
        pass_to_pass_success=sorted(p2p_ok),
        pass_to_pass_failure=sorted(p2p_bad),
        fail_to_pass_missing=sorted(f2p - seen),
        pass_to_pass_missing=sorted(p2p - seen),
    )


def build_eval_script(spec: GradingSpec, *, test_patch_path: str, patch_path: Optional[str]) -> str:
    """The upstream command sequence as one interactive-bash script (shell state is shared)."""
    rebuild = " ; ".join(spec.rebuild_cmds)
    test_cmd = " ; ".join(spec.test_cmds)
    print_cmd = " ; ".join(spec.print_cmds)
    lines = [
        "cd /testbed",
        # Some images keep the project one level below /testbed.
        '[ -d .git ] || { g=$(find . -maxdepth 2 -mindepth 2 -type d -name .git -print -quit); '
        '[ -n "$g" ] && cd "${g%/.git}"; } ;',
        f"git apply --reject --whitespace=nowarn {test_patch_path}; "
        f'echo "{APPLY_MARK} test_patch exit=$?"',
    ]
    if patch_path:
        lines.append(
            f"git apply --reject --whitespace=nowarn {patch_path}; "
            f'echo "{APPLY_MARK} patch exit=$?"'
        )
    lines.append("cd /testbed")
    if rebuild.strip():
        lines.append(rebuild)
    if not print_cmd.strip():
        # Python dataset: no print command; upstream wraps the test command this way.
        lines.append("cat > run_test.sh <<'BENCH_TEST_CMD'")
        lines.append(test_cmd)
        lines.append("BENCH_TEST_CMD")
        test_cmd = "bash run_test.sh > testlog.out 2>&1"
        print_cmd = "cat testlog.out"
    lines.append(test_cmd)
    lines.append(f'echo "{LOG_BEGIN}"')
    lines.append(print_cmd)
    return "\n".join(lines) + "\n"


@dataclass
class EvalResult:
    resolved: bool
    report: Optional[GradeReport] = None
    test_patch_applied: Optional[bool] = None
    patch_applied: Optional[bool] = None
    timed_out: bool = False
    empty_patch: bool = False
    duration_s: float = 0.0
    log: str = ""
    apply_log: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "empty_patch": self.empty_patch,
            "timed_out": self.timed_out,
            "test_patch_applied": self.test_patch_applied,
            "patch_applied": self.patch_applied,
            "apply_log": self.apply_log,
            "duration_s": round(self.duration_s, 3),
            "report": self.report.to_json() if self.report else None,
        }


def _apply_results(output: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for line in output.splitlines():
        if line.startswith(APPLY_MARK):
            name, _, code = line[len(APPLY_MARK) :].strip().partition(" exit=")
            out[name] = code.strip() == "0"
    return out


async def evaluate_patch(
    task: BenchTask,
    patch: str,
    *,
    backend: SandboxBackend,
    timeout_s: float = TEST_TIMEOUT_S,
) -> EvalResult:
    """Grade ``patch`` against ``task``'s hidden tests in a fresh sandbox of the instance image."""
    if not patch.strip():
        # Upstream counts an empty prediction as "empty patch" and never runs it.
        return EvalResult(resolved=False, empty_patch=True)
    spec = task.grading
    loop = asyncio.get_running_loop()
    started = loop.time()
    workspace = await SandboxWorkspace.create(task.image, backend=backend, agent=False)
    try:
        for path, content in (("/tmp/bench-test.diff", spec.test_patch), ("/tmp/bench-pred.diff", patch)):
            written = await workspace.exec(
                ["sh", "-c", f"cat > {path}"], stdin=content if content.endswith("\n") else content + "\n"
            )
            if written.exit_code != 0:
                raise GradingError(f"could not stage {path}: {written.stderr.strip()[-500:]}")
        script = build_eval_script(
            spec, test_patch_path="/tmp/bench-test.diff", patch_path="/tmp/bench-pred.diff"
        )
        try:
            run = await workspace.exec(
                ["bash", "-ic", script], env={"TERM": "xterm-mono"}, timeout=timeout_s
            )
        except SandboxExecTimeout:
            return EvalResult(
                resolved=False, timed_out=True, duration_s=loop.time() - started,
                log="evaluation timed out",
            )
    finally:
        await workspace.close()

    head, marker, log = run.stdout.partition(LOG_BEGIN)
    if not marker:
        raise GradingError(
            "evaluation script did not reach the test log: " + (run.stderr or run.stdout)[-1500:]
        )
    log = log.lstrip("\r\n")
    applied = _apply_results(head)
    apply_log = head + run.stderr
    missing = {"test_patch", "patch"} - applied.keys()
    if missing:
        raise GradingError(
            f"missing patch application status for {', '.join(sorted(missing))}: "
            + apply_log[-1500:]
        )
    if not applied["test_patch"]:
        raise GradingError("hidden test patch did not apply: " + apply_log[-1500:])
    status = await asyncio.to_thread(run_log_parser, spec.log_parser, log)
    report = grade(status, spec.fail_to_pass, spec.pass_to_pass)
    return EvalResult(
        resolved=applied["patch"] and report.resolved,
        report=report,
        test_patch_applied=applied.get("test_patch"),
        patch_applied=applied.get("patch"),
        duration_s=loop.time() - started,
        log=log,
        apply_log=apply_log,
    )

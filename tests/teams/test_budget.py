from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from agent.teams import Budget, BudgetExceeded, CodingTask, HarnessRun, LocalWorktree, Usage, build_team
from agent.teams import spec as spec_module
from agent.teams.budget import BudgetedHarness

from .fakes import FakeHarness, make_repo, run, verdict_json, write

TASK = CodingTask("t1", "make solution.txt contain the answer")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    return make_repo(tmp_path / "repo")


@dataclass
class CostedHarness(FakeHarness):
    """FakeHarness whose runs cost ``cost`` dollars (``None``: unpriced) and exit with ``exit_code``."""

    cost: Optional[float] = 0.01
    exit_code: int = 0

    async def run(self, workspace, prompt, **kwargs) -> HarnessRun:
        result = await super().run(workspace, prompt, **kwargs)
        result.usage = Usage(10, 0, 5, self.cost)
        result.exit_code = self.exit_code
        return result


async def noop(call):
    return "ok"


def a_run(cost: Optional[float], harness: str = "h", role: str = "worker") -> HarnessRun:
    return HarnessRun(harness, None, role, "", 0, Usage(cost_usd=cost), 0.0)


def solve(team, repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            return await team.solve(TASK, ws)

    return run(go())


def test_the_limit_must_be_positive():
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            Budget(bad)


def test_runs_are_refused_once_the_limit_is_reached_and_the_refusal_is_recorded():
    budget = Budget(0.05)
    budget.admit("claude", "coder")
    budget.charge(a_run(0.03))
    budget.admit("claude", "coder")  # 0.03 < 0.05: still admitted
    budget.charge(a_run(0.02))
    assert budget.exhausted and budget.spent_usd == pytest.approx(0.05)

    with pytest.raises(BudgetExceeded, match="claude"):
        budget.admit("claude", "reviewer")
    assert [(r["role"], r["harness"]) for r in budget.refused] == [("reviewer", "claude")]
    assert "used up" in budget.refused[0]["reason"]


def test_an_unpriced_run_blocks_later_runs_unless_allowed():
    strict = Budget(1.0)
    strict.charge(a_run(None))
    assert strict.unpriced_runs == 1 and strict.exhausted
    with pytest.raises(BudgetExceeded):
        strict.admit("codex", "worker")
    assert "unpriced" in strict.refused[0]["reason"]

    lenient = Budget(1.0, allow_unpriced=True)
    lenient.charge(a_run(None))
    lenient.admit("codex", "worker")
    assert lenient.unpriced_runs == 1 and lenient.spent_usd == 0.0 and not lenient.exhausted and not lenient.refused


def test_the_warning_fires_once_when_the_threshold_is_crossed():
    seen = []
    budget = Budget(1.0, on_warn=lambda b: seen.append(b.spent_usd))
    budget.charge(a_run(0.5))
    assert seen == [] and not budget.warned
    budget.charge(a_run(0.4))  # 0.9 of 1.0: past the 0.8 threshold
    budget.charge(a_run(0.05))
    assert seen == [pytest.approx(0.9)] and budget.warned


def test_to_dict_snapshots_the_state():
    budget = Budget(0.02)
    budget.charge(a_run(0.02))
    with pytest.raises(BudgetExceeded):
        budget.admit("codex", "worker")
    assert budget.to_dict() == {
        "max_cost_usd": 0.02,
        "spent_usd": 0.02,
        "unpriced_runs": 0,
        "warned": True,
        "exhausted": True,
        "refused": budget.refused,
    }
    assert len(budget.refused) == 1


def test_budgeted_harness_forwards_identity_and_run_keywords():
    seen = {}

    class Inner:
        name, model, supports_output_schema = "inner", "m1", True

        async def run(self, workspace, prompt, *, role="worker", session_id=None, timeout=None, output_schema=None):
            seen.update(role=role, session_id=session_id, timeout=timeout, output_schema=output_schema, prompt=prompt)
            return a_run(0.25, "inner", role)

    budget = Budget(1.0)
    harness = BudgetedHarness(Inner(), budget)
    assert (harness.name, harness.model, harness.supports_output_schema) == ("inner", "m1", True)
    assert BudgetedHarness(FakeHarness(noop), budget).supports_output_schema is False

    result = run(harness.run(None, "go", role="judge", session_id="s", timeout=3.0, output_schema={"type": "object"}))
    assert result.role == "judge" and budget.spent_usd == 0.25
    assert seen == {
        "role": "judge",
        "session_id": "s",
        "timeout": 3.0,
        "output_schema": {"type": "object"},
        "prompt": "go",
    }


def test_a_failed_run_is_charged_and_an_exception_is_not():
    budget = Budget(1.0)
    failing = BudgetedHarness(CostedHarness(noop, exit_code=1), budget)
    workspace = SimpleNamespace(root="/work")
    assert run(failing.run(workspace, "go")).exit_code == 1
    assert budget.spent_usd == pytest.approx(0.01)

    async def boom(call):
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        run(BudgetedHarness(FakeHarness(boom), budget).run(workspace, "go"))
    assert budget.spent_usd == pytest.approx(0.01) and not budget.refused


def patch_harnesses(monkeypatch, **harnesses):
    real = spec_module.build_harness

    def build(spec, **kw):
        name = spec.partition("@")[0]
        return harnesses[name] if name in harnesses else real(spec, **kw)

    monkeypatch.setattr(spec_module, "build_harness", build)


def numbered_coder():
    async def script(call):
        await write(call.workspace, "solution.txt", f"attempt {call.number}\n")
        return "done"

    return CostedHarness(script, name="coder")


def test_review_team_stops_paying_the_coder_once_the_budget_is_used_up(repo, monkeypatch):
    coder = numbered_coder()

    async def reviewer_script(call):
        return verdict_json(4, passed=False, feedback="not yet")

    reviewer = CostedHarness(reviewer_script, name="reviewer")
    patch_harnesses(monkeypatch, claude=coder, codex=reviewer)
    budget = Budget(0.02)

    result = solve(build_team("review:coder=claude,reviewer=codex,rounds=3", budget=budget), repo)

    # Round 1 spends $0.02 (coder + reviewer); round 2's coder is refused, nothing else runs.
    assert [r.role for r in result.runs] == ["coder", "reviewer"]
    assert len(coder.calls) == 1 and len(reviewer.calls) == 1
    assert "attempt 1" in result.patch
    assert budget.spent_usd == pytest.approx(0.02) and budget.exhausted
    assert [(r["role"], r["harness"]) for r in budget.refused] == [("coder", "coder")]
    failures = result.meta["failures"]
    assert [f["role"] for f in failures] == ["coder"] and "BudgetExceeded" in failures[0]["error"]


def test_a_refused_review_is_a_failure_not_a_run(repo, monkeypatch):
    coder = numbered_coder()
    reviewer = CostedHarness(noop, name="reviewer")
    patch_harnesses(monkeypatch, claude=coder, codex=reviewer)
    budget = Budget(0.01)  # the coder's first run uses it up

    result = solve(build_team("review:coder=claude,reviewer=codex", budget=budget), repo)

    assert [r.role for r in result.runs] == ["coder"] and reviewer.calls == []
    # The unreviewed attempt scores 0, so the coder is asked for a second round: refused too.
    assert sorted((f["role"], "BudgetExceeded" in f["error"]) for f in result.meta["failures"]) == [
        ("coder", True),
        ("reviewer", True),
    ]
    assert set(result.meta["scores"]) == {0.0}


def test_a_check_reviewer_is_never_wrapped_and_costs_nothing(repo, monkeypatch):
    coder = numbered_coder()
    patch_harnesses(monkeypatch, claude=coder)
    budget = Budget(0.01)

    team = build_team("review:coder=claude,reviewer=check", check="test -f solution.txt", budget=budget)
    result = solve(team, repo)

    assert isinstance(team.coder, BudgetedHarness) and not isinstance(team.reviewer, BudgetedHarness)
    assert result.meta["accepted"] is True and "attempt 1" in result.patch
    assert budget.spent_usd == pytest.approx(0.01) and not budget.refused
    assert "failures" not in result.meta


def test_without_a_budget_harnesses_are_not_wrapped(monkeypatch):
    coder = numbered_coder()
    patch_harnesses(monkeypatch, claude=coder)
    assert build_team("solo:worker=claude").worker is coder

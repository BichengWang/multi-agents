import json
from pathlib import Path

import pytest

from agent.teams import SoloTeam
from agent.teams.budget import BudgetedHarness
from agent.teams import cli as cli_module

from .fakes import FakeHarness, git, make_repo, write


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(repo)
    harness = FakeHarness(None, name="solo")

    async def script(call):
        await write(call.workspace, "solution.txt", "done\n")
        return "ok"

    harness.script = script
    monkeypatch.setattr(cli_module, "build_team", lambda spec, **kw: SoloTeam(harness, name=spec))
    return repo, harness, tmp_path


def test_apply_from_inside_the_repo_applies_the_patch_and_records_outside_the_tree(env):
    repo, harness, tmp_path = env
    code = cli_module.main(["run", "--team", "solo:worker=claude", "--apply", "make solution.txt"])
    assert code == 0
    assert (repo / "solution.txt").read_text() == "done\n"
    # The only change in the user's tree is the patch itself: no run record, no worktree.
    assert git(repo, "status", "--porcelain").strip() == "?? solution.txt"
    records = list((tmp_path / "cache" / "multi-agents" / "runs").glob("*/*/record.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["team"] == "solo:worker=claude" and "solution.txt" in record["patch"]
    assert (records[0].parent / "final.patch").read_text() == record["patch"]


def test_apply_refuses_a_dirty_tree_before_spending_anything(env):
    repo, harness, _ = env
    (repo / "mine.txt").write_text("work in progress\n")
    code = cli_module.main(["run", "--team", "solo:worker=claude", "--apply", "make solution.txt"])
    assert code != 0 and harness.calls == []
    assert not (repo / "solution.txt").exists() and (repo / "mine.txt").exists()


def test_without_apply_the_working_tree_is_untouched(env):
    repo, _, tmp_path = env
    assert cli_module.main(["run", "--team", "solo:worker=claude", "--runs-dir", str(tmp_path / "out"), "x"]) == 0
    assert git(repo, "status", "--porcelain").strip() == ""
    assert Path(tmp_path / "out").exists()


@pytest.fixture
def budgeted(env, monkeypatch):
    repo, harness, tmp_path = env
    monkeypatch.setattr(
        cli_module,
        "build_team",
        lambda spec, **kw: SoloTeam(
            BudgetedHarness(harness, kw["budget"]) if kw.get("budget") else harness, name=spec
        ),
    )
    return harness, tmp_path


def read_record(tmp_path):
    (path,) = (tmp_path / "cache" / "multi-agents" / "runs").glob("*/*/record.json")
    return json.loads(path.read_text())


def test_max_cost_is_recorded_and_summarized(budgeted, capsys):
    harness, tmp_path = budgeted
    assert cli_module.main(["run", "--team", "solo:worker=claude", "--max-cost", "1", "make solution.txt"]) == 0
    budget = read_record(tmp_path)["budget"]
    assert budget["max_cost_usd"] == 1.0 and budget["spent_usd"] == pytest.approx(0.01)
    assert budget["refused"] == [] and budget["exhausted"] is False
    out = capsys.readouterr().out
    assert "budget: $0.0100 of $1.0000 used" in out and "refused" not in out


def test_crossing_the_warning_threshold_is_announced(budgeted, capsys):
    assert cli_module.main(["run", "--team", "solo:worker=claude", "--max-cost", "0.011", "make solution.txt"]) == 0
    out = capsys.readouterr().out
    assert "Budget: $0.0100 of $0.0110 used" in out


def test_without_max_cost_the_record_has_no_budget(budgeted):
    _, tmp_path = budgeted
    assert cli_module.main(["run", "--team", "solo:worker=claude", "make solution.txt"]) == 0
    assert "budget" not in read_record(tmp_path)


def test_max_cost_rejects_an_unpriced_harness_before_any_agent_runs(budgeted, capsys):
    harness, _ = budgeted
    code = cli_module.main(["run", "--team", "solo:worker=codex", "--max-cost", "1", "make solution.txt"])
    assert code == 2 and harness.calls == []
    out = capsys.readouterr().out
    assert "unpriced: codex" in out and "--allow-unpriced" in out


def test_allow_unpriced_accepts_it_and_says_what_it_means(budgeted, capsys):
    harness, tmp_path = budgeted
    argv = ["run", "--team", "solo:worker=codex", "--max-cost", "1", "--allow-unpriced", "make solution.txt"]
    assert cli_module.main(argv) == 0
    assert len(harness.calls) == 1 and read_record(tmp_path)["budget"]["max_cost_usd"] == 1.0
    assert "count as $0" in capsys.readouterr().out


def test_allow_unpriced_without_max_cost_is_a_usage_error(budgeted):
    harness, _ = budgeted
    assert cli_module.main(["run", "--team", "solo:worker=claude", "--allow-unpriced", "make solution.txt"]) == 2
    assert harness.calls == []


@pytest.mark.parametrize("amount", ["0", "-3"])
def test_max_cost_must_be_positive(budgeted, amount):
    harness, _ = budgeted
    assert cli_module.main(["run", "--team", "solo:worker=claude", "--max-cost", amount, "x"]) == 2
    assert harness.calls == []

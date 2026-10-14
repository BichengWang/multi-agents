import json
from pathlib import Path

import pytest

from agent.teams import SoloTeam
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

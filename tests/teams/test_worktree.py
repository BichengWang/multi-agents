import asyncio
import os
import time

import pytest

from agent.teams import ExecTimeout, LocalWorktree

from .fakes import git, make_repo, run


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_AGENTS_WORKTREE_DIR", str(tmp_path / "worktrees"))
    return make_repo(tmp_path / "repo")


def test_worktree_is_isolated_from_the_user_tree(repo):
    head, branches = git(repo, "rev-parse", "HEAD"), git(repo, "branch", "--list")
    (repo / "dirty.txt").write_text("uncommitted\n")

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            assert not ws.root.startswith(str(repo))
            assert not os.path.exists(os.path.join(ws.root, "dirty.txt"))  # base commit only
            await ws.exec(["sh", "-c", "echo changed > a.txt && echo new > b.txt"])
            return ws.root

    root = run(go())
    assert git(repo, "status", "--porcelain").strip() == "?? dirty.txt"
    assert (repo / "a.txt").read_text() == "base\n"
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "branch", "--list") == branches
    assert not os.path.exists(root)
    assert git(repo, "worktree", "list").count("\n") == 1  # only the main tree is left


def test_diff_covers_new_binary_committed_files_and_skips_ignored(repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            await ws.exec(["sh", "-c", "echo changed > a.txt; echo new > b.txt; echo noise > x.log"])
            await ws.exec(["sh", "-c", "printf '\\000\\001\\377binary' > blob.bin"])
            await ws.exec(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "agent commit"])
            await ws.exec(["sh", "-c", "echo more >> b.txt"])
            patch = await ws.diff()
            staged = (await ws.exec(["git", "diff", "--cached", "--name-only"])).stdout
            return patch, staged

    patch, staged = run(go())
    assert staged == ""  # diff() leaves the worktree's own index alone
    assert "a.txt" in patch and "b.txt" in patch and "blob.bin" in patch
    assert "x.log" not in patch and "GIT binary patch" in patch


def test_apply_to_fork_reproduces_changes_and_fork_starts_at_base(repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            await ws.exec(["sh", "-c", "echo changed > a.txt; printf '\\000\\377' > blob.bin"])
            patch = await ws.diff()
            async with await ws.fork() as fork:
                assert fork.root != ws.root and (await fork.diff()) == ""
                await fork.apply(patch)
                assert (await fork.diff()) == patch
                data = open(os.path.join(fork.root, "blob.bin"), "rb").read()
            return data

    assert run(go()) == b"\x00\xff"


def test_exec_timeout_kills_the_whole_process_group(repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            with pytest.raises(ExecTimeout):
                await ws.exec(["sh", "-c", "sleep 300 & echo $! > pid; wait"], timeout=1)
            return int(open(os.path.join(ws.root, "pid")).read())

    pid = run(go())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancelled_exec_kills_its_process(repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            task = asyncio.create_task(ws.exec(["sh", "-c", "sleep 300 & echo $! > pid; wait"]))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if os.path.exists(os.path.join(ws.root, "pid")) and open(os.path.join(ws.root, "pid")).read():
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return int(open(os.path.join(ws.root, "pid")).read())

    pid = run(go())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_closing_the_original_also_closes_forks_left_open(repo):
    async def go():
        ws = await LocalWorktree.create(repo)
        fork = await ws.fork()
        await ws.close()
        return fork.root

    assert not os.path.exists(run(go()))
    assert git(repo, "worktree", "list").count("\n") == 1


def test_diff_round_trips_whatever_diff_prefix_config_the_user_has(repo, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'diff.noprefix=true'")

    async def go():
        async with await LocalWorktree.create(repo) as ws:
            await ws.exec(["sh", "-c", "echo changed > a.txt; echo new > sub_b.txt"])
            patch = await ws.diff()
            async with await ws.fork() as fork:
                await fork.apply(patch)
                return patch, await fork.diff()

    patch, again = run(go())
    assert "diff --git a/a.txt b/a.txt" in patch and again == patch


def test_repeated_event_loops_can_create_forks_concurrently(repo):
    async def go():
        async with await LocalWorktree.create(repo) as ws:
            forks = await asyncio.gather(ws.fork(), ws.fork(), ws.fork())
            for fork in forks:
                await fork.close()

    run(go())
    run(go())  # a second loop must not trip over locks bound to the first

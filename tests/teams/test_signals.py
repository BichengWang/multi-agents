import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .fakes import make_repo

ROOT = Path(__file__).resolve().parents[2]

# A team whose only worker starts a long-running grandchild in its own process group and waits.
SCRIPT = """
import sys
from agent.teams import SoloTeam, cli


class Sleeper:
    name = "sleeper"
    model = None

    async def run(self, workspace, prompt, *, role="worker", session_id=None, timeout=None):
        await workspace.exec(["sh", "-c", 'sleep 300 & echo $! > "$MARK"; wait'])
        raise AssertionError("the worker must be killed, not finish")


cli.build_team = lambda spec, **kw: SoloTeam(Sleeper(), name=spec)
sys.exit(cli.main(["run", "--team", "solo:worker=claude", "--repo", sys.argv[1], "--runs-dir", sys.argv[2], "x"]))
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed grandchild lingers as a zombie until its (re-parented) parent is reaped.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


@pytest.mark.parametrize(
    "sig,code",
    [(signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130)],
)
def test_a_signal_cancels_the_team_kills_its_workers_and_removes_the_worktrees(tmp_path, sig, code):
    repo = make_repo(tmp_path / "repo")
    script = tmp_path / "run_team.py"
    script.write_text(SCRIPT)
    mark = tmp_path / "pid"
    worktrees = tmp_path / "worktrees"
    env = {
        **os.environ,
        "MARK": str(mark),
        "MULTI_AGENTS_WORKTREE_DIR": str(worktrees),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, str(script), str(repo), str(tmp_path / "runs")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not (mark.exists() and mark.read_text().strip()):
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the worker never started"
            time.sleep(0.05)
        pid = int(mark.read_text())
        assert alive(pid) and any(worktrees.glob("*/*"))

        proc.send_signal(sig)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == code, proc.stdout.read()

    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(pid)
    assert not list(worktrees.glob("*/*"))
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert len(listing.strip().splitlines()) == 1

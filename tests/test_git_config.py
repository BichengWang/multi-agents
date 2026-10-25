import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("config_scope", ["GLOBAL", "SYSTEM"])
def test_git_operations_ignore_host_config(tmp_path, config_scope):
    hooks = tmp_path / "host-hooks"
    hooks.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text(
        f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n"
    )
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text('''
import subprocess


def test_commit_and_push_hook(tmp_path):
    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=check, capture_output=True, text=True
        )

    git("init", "-q", "-b", "main")
    (tmp_path / "file.txt").write_text("content")
    git("add", "file.txt")
    git("commit", "-qm", "initial")
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin))
    git("remote", "add", "origin", str(origin))
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\\nexit 1\\n")
    hook.chmod(0o755)
    assert git("push", "origin", "main", check=False).returncode != 0
    assert git("--git-dir", str(origin), "show-ref", check=False).returncode == 1
''')
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "0",
        f"GIT_CONFIG_{config_scope}": str(config),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_probe.py", "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

import os

import pytest

# Agent modules may build OpenAI clients at import time; tests never call the API.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch):
    """Keep host git settings out of tests and the subprocesses they exercise."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")

import asyncio

import pytest

from agent.bench.sandbox import (
    CLAUDE_CODE_VERSION,
    CODEX_VERSION,
    NODE_VERSION,
    ModalBackend,
    ModalSandboxWorkspace,
    SandboxConfig,
    SandboxError,
    SandboxExecTimeout,
)
from agent.teams.types import Workspace

from .fakes import FakeModal, make_repo
from .repos import FIXED, make_template


def prepare(tmp_path, *, config=None):
    template, base, fix = make_template(tmp_path)
    (tmp_path / "work").mkdir(exist_ok=True)
    modal = FakeModal(template, tmp_path / "work")
    backend = ModalBackend(config or SandboxConfig(), modal=modal)
    return modal, backend, base, fix


def run(coro):
    return asyncio.run(coro)


async def sh(ws, script, **kwargs):
    result = await ws.exec(["sh", "-c", script], **kwargs)
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_workspace_satisfies_the_contract_and_diff_has_local_worktree_semantics(tmp_path):
    modal, backend, base, _ = prepare(tmp_path)

    async def scenario():
        ws = await ModalSandboxWorkspace.create("img:1", backend=backend)
        assert isinstance(ws, Workspace)
        assert ws.head == base
        # Leftovers from the image build are part of the baseline, not of the agent's patch.
        assert await ws.diff() == ""

        await sh(ws, "printf 'def add(a, b):\\n    return a + b\\n' > calc.py")
        await sh(ws, "echo new > pkg/new.txt && printf '\\000\\001\\002binary' > blob.bin")
        await sh(ws, "rm notes.txt && echo noise > debug.log && echo more >> leftover.txt")
        patch = await ws.diff()
        assert "+    return a + b" in patch
        assert "pkg/new.txt" in patch and "new file mode" in patch
        assert "GIT binary patch" in patch and "blob.bin" in patch
        assert "deleted file mode" in patch and "notes.txt" in patch
        assert "debug.log" not in patch  # .gitignore honoured
        assert "build/out.bin" not in patch  # pre-existing image state
        assert "+more" in patch  # but later edits to image leftovers are the agent's
        assert "index " in patch and len(patch.split("index ")[1].split("..")[0]) == 40  # --full-index

        # Commits the agent makes do not hide changes: the baseline is the base state.
        await sh(ws, "git add -A && git commit -qm agent-work")
        assert await ws.diff() == patch

        fork = await ws.fork()
        assert fork is not ws and isinstance(fork, Workspace)
        assert len(modal.sandboxes) == 2 and modal.sandboxes[0] is not modal.sandboxes[1]
        assert await fork.diff() == ""
        assert (await fork.exec(["cat", "calc.py"])).stdout.endswith("return a - b\n")
        await fork.apply(patch)
        assert await fork.diff() == patch
        assert (await fork.exec(["cat", "calc.py"])).stdout == FIXED
        assert (await fork.exec(["cat", "blob.bin"])).stdout.endswith("binary")
        assert (await fork.exec(["test", "-e", "notes.txt"])).exit_code == 1

        await fork.apply("")  # no-op
        with pytest.raises(SandboxError, match="git apply failed"):
            await fork.apply("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-nope\n+x\n")

        await ws.close()
        await ws.close()  # idempotent
        await fork.close()
        assert all(sb.terminated for sb in modal.sandboxes)

    run(scenario())


def test_exec_merges_env_pipes_stdin_returns_exit_codes_and_maps_timeouts(tmp_path):
    _, backend, _, _ = prepare(tmp_path)

    async def scenario():
        async with await ModalSandboxWorkspace.create("img:1", backend=backend) as ws:
            out = await ws.exec(["sh", "-c", "echo $FOO $IS_SANDBOX"], env={"FOO": "bar"})
            assert out.stdout == "bar 1\n"  # per-call env over the sandbox env
            echoed = await ws.exec(["cat"], stdin="héllo wörld\n" * 1000)
            assert echoed.stdout == "héllo wörld\n" * 1000
            failed = await ws.exec(["sh", "-c", "echo oops >&2; exit 3"])
            assert (failed.exit_code, failed.stderr) == (3, "oops\n")
            with pytest.raises(SandboxExecTimeout) as info:
                await ws.exec(["sleep", "5"], timeout=0.2)
            assert isinstance(info.value, TimeoutError)
            assert (await ws.exec(["pwd"])).stdout.strip() == ws.root

    run(scenario())


def test_agent_and_grader_sandboxes_are_configured_differently(tmp_path):
    config = SandboxConfig(secret_name="keys", secret_keys=("ANTHROPIC_API_KEY",))
    modal, backend, _, _ = prepare(tmp_path, config=config)

    async def scenario():
        agent = await ModalSandboxWorkspace.create("img:1", backend=backend)
        grader = await ModalSandboxWorkspace.create("img:1", backend=backend, agent=False)
        await agent.close()
        await grader.close()

    run(scenario())
    agent_call, grader_call = modal.created
    assert agent_call["args"] == ("sleep", "14400")
    assert agent_call["env"]["IS_SANDBOX"] == "1" and agent_call["env"]["CI"] == "1"
    assert agent_call["secrets"] and modal.secrets == [("keys", ["ANTHROPIC_API_KEY"])]
    assert (agent_call["cpu"], agent_call["memory"], agent_call["timeout"]) == (2.0, 16384, 14400)
    image = agent_call["image"]
    assert image.tag == "img:1" and len(image.commands) == 1
    (install,) = image.commands
    for pinned in (
        f"NODE={NODE_VERSION}\n",
        "node-v$NODE-linux-x64.tar.gz",
        f"@anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}",
        f"@openai/codex@{CODEX_VERSION}",
        "sha256sum -c",
    ):
        assert pinned in install
    # Grading uses the unmodified instance image and never sees API keys.
    assert grader_call["image"].commands == []
    assert "secrets" not in grader_call or not grader_call["secrets"]
    assert "IS_SANDBOX" not in grader_call["env"]


def test_git_history_is_stripped_so_the_fix_cannot_be_read(tmp_path):
    modal, backend, base, fix = prepare(tmp_path)
    keep = ModalBackend(SandboxConfig(isolate_history=False), modal=modal)

    async def history(ws):
        log = await sh(ws, "git log --all --format=%H")
        tags = await sh(ws, "git tag")
        branches = await sh(ws, "git branch -a --format='%(refname)'")
        has_fix = (await ws.exec(["git", "cat-file", "-e", fix])).exit_code == 0
        return log.split(), tags.split(), branches.split(), has_fix

    async def scenario():
        async with await ModalSandboxWorkspace.create("img:1", backend=backend) as ws:
            log, tags, branches, has_fix = await history(ws)
            assert log == [base] and tags == [] and not has_fix
            assert [b for b in branches if "fix" in b] == []
            assert await ws.diff() == ""  # still a working baseline after the gc
        async with await ModalSandboxWorkspace.create("img:1", backend=keep) as ws:
            log, tags, _, has_fix = await history(ws)
            assert fix in log and tags == ["v2"] and has_fix

    run(scenario())


def test_repository_nested_below_testbed_is_found(tmp_path):
    template_parent = tmp_path / "image"
    make_repo(template_parent, {"a.txt": "1\n"}, nested="project")
    (tmp_path / "work").mkdir()
    modal = FakeModal(template_parent, tmp_path / "work")
    backend = ModalBackend(SandboxConfig(), modal=modal)

    async def scenario():
        async with await ModalSandboxWorkspace.create("img:1", backend=backend) as ws:
            assert ws.root.endswith("/project")
            await sh(ws, "echo 2 >> a.txt")
            assert "+2" in await ws.diff()

    run(scenario())


def test_failed_setup_terminates_the_sandbox(tmp_path):
    plain = tmp_path / "image"
    plain.mkdir()
    (plain / "file.txt").write_text("not a repository")
    (tmp_path / "work").mkdir()
    modal = FakeModal(plain, tmp_path / "work")
    backend = ModalBackend(SandboxConfig(), modal=modal)

    with pytest.raises(SandboxError, match="setup failed"):
        run(ModalSandboxWorkspace.create("img:1", backend=backend))
    assert len(modal.sandboxes) == 1 and modal.sandboxes[0].terminated


def test_registry_secret_and_python_injection_are_optional_image_arguments(tmp_path):
    template, _, _ = make_template(tmp_path)
    (tmp_path / "work").mkdir()
    modal = FakeModal(template, tmp_path / "work")
    backend = ModalBackend(
        SandboxConfig(registry_secret_name="hub", add_python="3.12"), modal=modal
    )
    image = backend.image("img:9", agent_tools=False)
    assert image.kwargs["add_python"] == "3.12" and image.kwargs["secret"].secret == "hub"
    assert modal.secrets == [("hub", ["REGISTRY_USERNAME", "REGISTRY_PASSWORD"])]

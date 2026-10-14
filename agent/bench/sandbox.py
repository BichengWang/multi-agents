"""Modal Sandbox implementation of the ``Workspace`` contract.

Agent workspaces run in the instance's own Docker image (the exact environment the dataset's tests
were validated in) extended with a pinned Node.js and the ``claude`` / ``codex`` CLIs. API keys come
from a Modal Secret. Grading uses the plain instance image with no CLIs and no secrets.

Semantics match ``agent.teams.worktree.LocalWorktree``:

* ``diff`` is everything since the workspace was created (commits, edits, new files; ``.gitignore``
  honoured), computed with a throwaway git index, binary-safe (``--binary --full-index``);
* ``apply`` is ``git apply --binary``;
* ``fork`` is an independent sandbox from the same image, at its pristine state.

The baseline for ``diff`` is a tree object captured right after the sandbox starts, not ``HEAD``:
instance images carry build output and sometimes tracked-file changes from environment setup, and
those must not show up in the agent's patch.

Nothing here is executed against Modal in the offline test-suite; the tests drive the same code
through a fake Modal client that runs commands in a local temporary git repository.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
import shlex
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.teams.types import ExecResult

from .redact import Redactor

# Pinned toolchain baked into the derived image. Bump deliberately; the Modal image cache key
# includes these strings, so a bump rebuilds every instance image once.
NODE_VERSION = "24.21.0"
CLAUDE_CODE_VERSION = "2.1.287"
CODEX_VERSION = "0.162.1"

SECRET_HELP = (
    "Create the Modal Secret once with the keys your teams need, e.g.\n"
    "  modal secret create multi-agents-bench ANTHROPIC_API_KEY=... OPENAI_API_KEY=... "
    "[OPENROUTER_API_KEY=...]"
)


class SandboxExecTimeout(TimeoutError):
    """``Workspace.exec`` exceeded its timeout; the remote process was killed by Modal."""

    def __init__(self, argv: Sequence[str], timeout: float) -> None:
        super().__init__(f"command timed out after {timeout:g}s: {' '.join(argv[:3])}")
        self.argv = list(argv)
        self.timeout = timeout


class SandboxError(RuntimeError):
    """Sandbox creation, setup or a workspace git operation failed."""


@dataclass(frozen=True)
class SandboxConfig:
    app_name: str = "multi-agents-bench"
    # Modal Secret injected into agent sandboxes (never into grading sandboxes).
    secret_name: str = "multi-agents-bench"
    # Keys that must exist in the secret; Modal fails fast and names the missing ones.
    secret_keys: tuple[str, ...] = ()
    # Optional Modal Secret with REGISTRY_USERNAME / REGISTRY_PASSWORD for Docker Hub pulls.
    registry_secret_name: Optional[str] = None
    # Dataset-recommended resources are 4 CPUs / 16 GB (some C++ repos need more). Modal counts
    # physical cores (2 vCPU each), so 2.0 cores is the 4 CPUs.
    cpu: float = 2.0
    memory_mb: int = 16384
    # Maximum sandbox lifetime (Modal allows up to 24 h).
    lifetime_s: int = 4 * 3600
    # Applied when a caller passes no timeout, so a hung command can never run unbounded.
    default_exec_timeout_s: float = 2 * 3600
    # Only for instance images without Python on PATH, if Modal rejects them as-is.
    add_python: Optional[str] = None
    # Strip refs, remotes, reflogs and unreachable objects so the fix commit cannot be read from
    # git history inside the image.
    isolate_history: bool = True
    # Refuse images whose checked-out commit is not the task's base_commit (a dataset/image
    # mismatch would otherwise be graded silently).
    check_base_commit: bool = True
    node_version: str = NODE_VERSION
    claude_code_version: str = CLAUDE_CODE_VERSION
    codex_version: str = CODEX_VERSION
    # Extra environment for every process in agent sandboxes. IS_SANDBOX=1 lets Claude Code run
    # with permissions bypassed as root.
    env: dict[str, str] = field(default_factory=dict)


_SANDBOX_ENV = {
    "IS_SANDBOX": "1",
    "CI": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}

_INSTALL_TOOLS = r"""
set -eu
NODE=__NODE__
NODE_DIR=/opt/node-v$NODE-linux-x64
have() { command -v "$1" >/dev/null 2>&1; }
if ! have curl && ! have wget; then
  if have apt-get; then
    apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates
  elif have apk; then
    apk add --no-cache curl ca-certificates
  elif have dnf; then
    dnf install -y curl ca-certificates
  elif have yum; then
    yum install -y curl ca-certificates
  else
    echo "no curl/wget and no known package manager" >&2; exit 1
  fi
fi
fetch() { if have curl; then curl -fsSL "$1" -o "$2"; else wget -q -O "$2" "$1"; fi; }
cd /tmp
fetch "https://nodejs.org/dist/v$NODE/node-v$NODE-linux-x64.tar.gz" "node-v$NODE-linux-x64.tar.gz"
fetch "https://nodejs.org/dist/v$NODE/SHASUMS256.txt" SHASUMS256.txt
grep " node-v$NODE-linux-x64.tar.gz\$" SHASUMS256.txt | sha256sum -c -
tar -xzf "node-v$NODE-linux-x64.tar.gz" -C /opt
rm -f "node-v$NODE-linux-x64.tar.gz" SHASUMS256.txt
for b in node npm npx; do ln -sf "$NODE_DIR/bin/$b" /usr/local/bin/$b; done
npm install -g --no-audit --no-fund \
  "@anthropic-ai/claude-code@__CLAUDE__" "@openai/codex@__CODEX__"
npm cache clean --force >/dev/null 2>&1 || true
for b in claude codex; do ln -sf "$NODE_DIR/bin/$b" /usr/local/bin/$b; done
node --version && claude --version && codex --version
"""


def install_script(config: SandboxConfig) -> str:
    return (
        _INSTALL_TOOLS.replace("__NODE__", config.node_version)
        .replace("__CLAUDE__", config.claude_code_version)
        .replace("__CODEX__", config.codex_version)
    )


# Finds the git root (some images keep the repository one level below /testbed), optionally
# removes everything that could reveal the fix, then records the baseline tree for `diff`.
_SETUP = r"""
set -e
git config --global --add safe.directory '*'
git config --global user.email "bench@localhost"
git config --global user.name "bench"
# No background gc/maintenance: it can prune objects while `diff` is staging a throwaway index.
git config --global gc.auto 0
git config --global maintenance.auto false
cd /testbed
if [ ! -d .git ]; then
  g=$(find . -maxdepth 2 -mindepth 2 -type d -name .git -print -quit)
  if [ -n "$g" ]; then cd "${g%/.git}"; fi
fi
git rev-parse --is-inside-work-tree >/dev/null
ROOT=$(pwd -P)
GITDIR=$(git rev-parse --absolute-git-dir)
HEAD_SHA=$(git rev-parse HEAD)
if [ "$1" = 1 ]; then
  git update-ref --no-deref HEAD "$HEAD_SHA"
  git for-each-ref --format='%(refname)' | while read -r ref; do git update-ref -d "$ref"; done
  for r in $(git remote); do git remote remove "$r"; done
  rm -f "$GITDIR/ORIG_HEAD" "$GITDIR/FETCH_HEAD"
  git reflog expire --expire=now --all
  # No `|| true`: if the fix objects cannot be pruned the pair must error, not leak silently.
  git gc --prune=now --quiet
fi
# Files untracked-but-not-ignored in the image (build output, generated reports) are baseline
# state, not the agent's work: they stay out of the baseline tree and out of every diff.
git ls-files --others --exclude-standard -z > "$GITDIR/bench-untracked"
IDX="$GITDIR/bench-base-index"
rm -f "$IDX"
GIT_INDEX_FILE="$IDX" git read-tree HEAD
GIT_INDEX_FILE="$IDX" git add --all
if [ -s "$GITDIR/bench-untracked" ]; then
  GIT_INDEX_FILE="$IDX" git update-index --force-remove -z --stdin < "$GITDIR/bench-untracked"
fi
TREE=$(GIT_INDEX_FILE="$IDX" git write-tree)
rm -f "$IDX"
echo "BENCH_ROOT=$ROOT"
echo "BENCH_HEAD=$HEAD_SHA"
echo "BENCH_TREE=$TREE"
"""

_DIFF = r"""
set -e
GITDIR=$(git rev-parse --absolute-git-dir)
IDX="$GITDIR/bench-diff-index"
export GIT_INDEX_FILE="$IDX"
trap 'rm -f "$IDX"' EXIT
git read-tree "$1"
git add --all
if [ -s "$GITDIR/bench-untracked" ]; then
  git update-index --force-remove -z --stdin < "$GITDIR/bench-untracked"
fi
git diff --cached --binary --full-index --no-color --no-ext-diff --no-renames "$1"
"""


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a Modal API through its ``.aio`` variant when present (fakes may be plain async)."""
    aio = getattr(fn, "aio", None)
    result = (aio or fn)(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def load_modal() -> Any:
    try:
        return importlib.import_module("modal")
    except ImportError as exc:
        raise SandboxError("modal is not installed: uv sync --extra bench") from exc


class ModalBackend:
    """Owns the Modal app, secret and image definitions shared by all sandboxes of a run."""

    def __init__(self, config: Optional[SandboxConfig] = None, *, modal: Any = None) -> None:
        self.config = config or SandboxConfig()
        self.modal = modal if modal is not None else load_modal()
        self.redactor = Redactor()
        self._app: Any = None
        self._app_lock = asyncio.Lock()

    async def app(self) -> Any:
        async with self._app_lock:
            if self._app is None:
                self._app = await _call(
                    self.modal.App.lookup, self.config.app_name, create_if_missing=True
                )
            return self._app

    def image(self, tag: str, *, agent_tools: bool) -> Any:
        kwargs: dict[str, Any] = {}
        if self.config.add_python:
            kwargs["add_python"] = self.config.add_python
        if self.config.registry_secret_name:
            kwargs["secret"] = self.modal.Secret.from_name(
                self.config.registry_secret_name,
                required_keys=["REGISTRY_USERNAME", "REGISTRY_PASSWORD"],
            )
        image = self.modal.Image.from_registry(tag, **kwargs)
        if agent_tools:
            image = image.run_commands(f"sh -c {shlex.quote(install_script(self.config))}")
        return image

    async def create_sandbox(self, tag: str, *, agent: bool) -> Any:
        cfg = self.config
        kwargs: dict[str, Any] = {}
        env: dict[str, str] = {}
        if agent:
            kwargs["secrets"] = [
                self.modal.Secret.from_name(
                    cfg.secret_name, required_keys=list(cfg.secret_keys)
                )
            ]
            env = {**_SANDBOX_ENV, **cfg.env}
        else:
            env = {"TERM": "xterm-mono", "DEBIAN_FRONTEND": "noninteractive"}
        return await _call(
            self.modal.Sandbox.create,
            "sleep",
            str(cfg.lifetime_s),
            app=await self.app(),
            image=self.image(tag, agent_tools=agent),
            env=env,
            cpu=cfg.cpu,
            memory=cfg.memory_mb,
            timeout=cfg.lifetime_s,
            **kwargs,
        )


def _is_exec_timeout(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.TimeoutError) or type(exc).__name__ in {
        "ExecTimeoutError",
        "TimeoutError",
    }


class ModalSandboxWorkspace:
    """A ``Workspace`` backed by one Modal Sandbox created from an instance image."""

    def __init__(
        self,
        sandbox: Any,
        *,
        backend: ModalBackend,
        image_tag: str,
        agent: bool,
        root: str,
        head: str,
        base_tree: str,
        isolate_history: bool,
        expected_commit: Optional[str] = None,
    ) -> None:
        self._sandbox = sandbox
        self._backend = backend
        self.image_tag = image_tag
        self.agent = agent
        self.root = root
        self.head = head  # the commit the image was checked out at
        self.base_tree = base_tree
        self.isolate_history = isolate_history
        self.expected_commit = expected_commit
        self._closed = False

    @classmethod
    async def create(
        cls,
        image_tag: str,
        *,
        backend: Optional[ModalBackend] = None,
        agent: bool = True,
        expected_commit: Optional[str] = None,
    ) -> ModalSandboxWorkspace:
        """Start a sandbox from ``image_tag``.

        ``agent=True`` adds the pinned CLIs and the API-key secret, finds the git root, strips git
        history (per config), verifies the checkout is ``expected_commit`` and records the
        baseline for ``diff``. ``agent=False`` is the grader: the unmodified instance image, no
        secrets, no setup; ``exec`` runs in ``/testbed`` and the evaluation script locates the
        repository itself, as upstream does.
        """
        backend = backend or ModalBackend()
        sandbox = await backend.create_sandbox(image_tag, agent=agent)
        if not agent:
            return cls(
                sandbox, backend=backend, image_tag=image_tag, agent=False,
                root="/testbed", head="", base_tree="", isolate_history=False,
            )
        cfg = backend.config
        try:
            isolate = cfg.isolate_history
            code, stdout, stderr = await cls._raw_exec(
                sandbox,
                cfg,
                ["sh", "-c", _SETUP, "sh", "1" if isolate else "0"],
                workdir="/",
                timeout=cfg.default_exec_timeout_s,
            )
            facts = dict(
                line.split("=", 1) for line in stdout.decode().splitlines() if line.startswith("BENCH_")
            )
            if code != 0 or "BENCH_TREE" not in facts:
                raise SandboxError(
                    f"workspace setup failed in {image_tag} (exit {code}): "
                    f"{stderr.decode('utf-8', 'replace').strip()[-2000:]}"
                )
            head = facts["BENCH_HEAD"]
            if cfg.check_base_commit and expected_commit and not (
                head.startswith(expected_commit) or expected_commit.startswith(head)
            ):
                raise SandboxError(
                    f"image {image_tag} is checked out at {head}, but the task's base_commit is "
                    f"{expected_commit}"
                )
            # Learn the secret values this sandbox holds so stored artifacts can be scrubbed.
            _, env_dump, _ = await cls._raw_exec(sandbox, cfg, ["env"], workdir="/", timeout=60)
            backend.redactor.add_env_dump(
                env_dump.decode("utf-8", "replace"),
                ignore=[*_SANDBOX_ENV.values(), *cfg.env.values()],
            )
        except BaseException:
            await _terminate(sandbox)
            raise
        return cls(
            sandbox,
            backend=backend,
            image_tag=image_tag,
            agent=True,
            root=facts["BENCH_ROOT"],
            head=head,
            base_tree=facts["BENCH_TREE"],
            isolate_history=isolate,
            expected_commit=expected_commit,
        )

    @staticmethod
    async def _raw_exec(
        sandbox: Any,
        config: SandboxConfig,
        argv: Sequence[str],
        *,
        workdir: str,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        stdin: Optional[bytes] = None,
    ) -> tuple[int, bytes, bytes]:
        limit = timeout if timeout is not None else config.default_exec_timeout_s
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            proc = await _call(
                sandbox.exec,
                *argv,
                workdir=workdir,
                env=env or None,
                timeout=max(1, math.ceil(limit)),
                text=False,
            )
            if stdin is not None:
                proc.stdin.write(stdin)
                proc.stdin.write_eof()
                await _call(proc.stdin.drain)
            # Modal enforces `timeout` remotely; the client-side bound only covers a stuck stream.
            out, err, code = await asyncio.wait_for(
                asyncio.gather(
                    _call(proc.stdout.read), _call(proc.stderr.read), _call(proc.wait)
                ),
                timeout=limit + 60,
            )
        except SandboxExecTimeout:
            raise
        except Exception as exc:
            if _is_exec_timeout(exc):
                raise SandboxExecTimeout(argv, limit) from exc
            raise
        code = int(code)
        # modal-client (py/v1.6.0, container_process.py) swallows its ExecTimeoutError: the
        # streams just end and ``wait()`` returns -1. Treat that, or a SIGKILL right at the
        # deadline, as the timeout it is, never as an ordinary failing command.
        if code == -1 or (code == 137 and loop.time() - started >= limit):
            raise SandboxExecTimeout(argv, limit)
        return code, _as_bytes(out), _as_bytes(err)

    async def exec(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        stdin: Optional[str] = None,
    ) -> ExecResult:
        code, out, err = await self._raw_exec(
            self._sandbox,
            self._backend.config,
            argv,
            workdir=self.root,
            env=env,
            timeout=timeout,
            stdin=stdin.encode("utf-8", "surrogateescape") if stdin is not None else None,
        )
        return ExecResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def _git_script(
        self, script: str, *args: str, stdin: Optional[bytes] = None
    ) -> tuple[int, bytes, bytes]:
        return await self._raw_exec(
            self._sandbox,
            self._backend.config,
            ["sh", "-c", script, "sh", *args],
            workdir=self.root,
            stdin=stdin,
        )

    async def diff(self) -> str:
        if not self.base_tree:
            raise SandboxError("grading sandboxes have no baseline to diff against")
        code, out, err = await self._git_script(_DIFF, self.base_tree)
        if code != 0:
            raise SandboxError(f"git diff failed ({code}): {err.decode('utf-8', 'replace').strip()}")
        return out.decode("utf-8", "surrogateescape")

    async def apply(self, patch: str) -> None:
        if not patch.strip():
            return
        code, _, err = await self._raw_exec(
            self._sandbox,
            self._backend.config,
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            workdir=self.root,
            stdin=patch.encode("utf-8", "surrogateescape"),
        )
        if code != 0:
            raise SandboxError(f"git apply failed ({code}): {err.decode('utf-8', 'replace').strip()}")

    async def fork(self) -> ModalSandboxWorkspace:
        return await self.create(
            self.image_tag,
            backend=self._backend,
            agent=self.agent,
            expected_commit=self.expected_commit,
        )

    async def close(self) -> None:
        """Terminate the sandbox. ``_closed`` is set only once termination succeeded, so a failed
        attempt (leaving a billed sandbox running) can be retried by a later ``close``."""
        if self._closed:
            return
        for attempt in range(3):
            if await _terminate(self._sandbox):
                self._closed = True
                return
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        warnings.warn(
            f"could not terminate sandbox for {self.image_tag}; it keeps running until its "
            f"{self._backend.config.lifetime_s}s lifetime ends",
            RuntimeWarning,
            stacklevel=2,
        )

    async def __aenter__(self) -> ModalSandboxWorkspace:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _as_bytes(value: Any) -> bytes:
    return value.encode("utf-8", "surrogateescape") if isinstance(value, str) else bytes(value)


async def _terminate(sandbox: Any) -> bool:
    """Terminate even when the caller is being cancelled. True on success; never raises."""
    task = asyncio.ensure_future(_call(sandbox.terminate))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        return False
    return True

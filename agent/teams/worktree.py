"""Local git-worktree workspace.

Each workspace is a detached ``git worktree`` of the user's repository at a fixed base commit.

Where the worktrees live: under the user cache directory
(``$MULTI_AGENTS_WORKTREE_DIR`` or ``$XDG_CACHE_HOME/multi-agents/worktrees`` or
``~/.cache/multi-agents/worktrees``), grouped per repository. That keeps them

* outside the user's working tree, so editors, file watchers and ``git status`` never see them;
* outside the ``.git`` directory, because coding-agent CLIs treat any path containing a ``.git``
  segment as protected and refuse or prompt on writes there;
* on a stable, discoverable path, so ``--keep-worktrees`` leaves something inspectable.

Creating a worktree only adds metadata under ``<git-common-dir>/worktrees``; it never touches the
user's working tree, index, HEAD or branches. Uncommitted changes in the user's tree are *not*
part of the base commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import uuid
import weakref
from pathlib import Path
from typing import Optional

from .types import GENERATED_FILES, ExecResult, Workspace

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

# `git worktree add/remove/prune` mutate shared repository metadata; serialize them per repo.
# Locks are bound to an event loop, so they are keyed by (loop, repo) and dropped with the loop.
_repo_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def _repo_lock(common_dir: str) -> asyncio.Lock:
    locks = _repo_locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(common_dir, asyncio.Lock())


class ExecTimeout(TimeoutError):
    """A ``Workspace.exec`` call exceeded its timeout; the process group was killed."""

    def __init__(self, argv: list[str], timeout: float, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"command timed out after {timeout:g}s: {' '.join(argv[:3])}")
        self.argv = argv
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


class GitError(RuntimeError):
    pass


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def run_process(
    argv: list[str],
    *,
    cwd: str,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    stdin: Optional[bytes] = None,
) -> tuple[int, bytes, bytes]:
    """Run ``argv`` in its own process group; timeout or cancellation kills the whole group."""
    create = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    try:
        proc = await asyncio.shield(create)
    except asyncio.CancelledError:
        # The child can already be running (and spawning) before creation returns. Cancelling
        # creation would kill only the leader and orphan its descendants, so finish creating it
        # and kill the whole group.
        with contextlib.suppress(BaseException):
            proc = await create
            _kill_group(proc)
            await proc.wait()
        raise
    comm = asyncio.ensure_future(proc.communicate(stdin))
    try:
        done, _ = await asyncio.wait({comm}, timeout=timeout)
        if not done:
            _kill_group(proc)
            out, err = await comm
            raise ExecTimeout(
                argv,
                timeout or 0.0,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"),
            )
        out, err = comm.result()
        # Descendants that outlived the leader (and kept the pipes open) are not our business,
        # but make sure nothing from this group lingers once the command has finished.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return proc.returncode if proc.returncode is not None else -1, out, err
    except asyncio.CancelledError:
        _kill_group(proc)
        comm.cancel()
        with contextlib.suppress(BaseException):
            await proc.wait()
        raise


async def _git(
    cwd: str,
    *args: str,
    env: Optional[dict[str, str]] = None,
    stdin: Optional[bytes] = None,
    check: bool = True,
) -> tuple[int, bytes, bytes]:
    code, out, err = await run_process(
        ["git", *args], cwd=cwd, env={**_GIT_ENV, **(env or {})}, stdin=stdin
    )
    if check and code != 0:
        raise GitError(f"git {' '.join(args[:3])} failed ({code}): {err.decode('utf-8', 'replace').strip()}")
    return code, out, err


def user_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/multi-agents`` (default ``~/.cache/multi-agents``)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "multi-agents"


def _cache_root() -> Path:
    override = os.environ.get("MULTI_AGENTS_WORKTREE_DIR")
    return Path(override) if override else user_cache_dir() / "worktrees"


def repo_key(repo: str, common_dir: str) -> str:
    """Stable, readable per-repository directory name: ``<name>-<hash of the git dir>``."""
    return f"{Path(repo).name}-{hashlib.sha1(common_dir.encode()).hexdigest()[:8]}"


class LocalWorktree:
    """A detached git worktree at ``base_commit``; see the module docstring for placement."""

    def __init__(
        self,
        root: str,
        *,
        repo: str,
        common_dir: str,
        base_commit: str,
        family: Optional[list[LocalWorktree]] = None,
    ) -> None:
        self.root = root
        self.repo = repo
        self.common_dir = common_dir
        self.base_commit = base_commit
        self._closed = False
        # The original worktree and every fork made from it. Closing the original also closes
        # forks still open, so an interrupted team cannot leak worktrees.
        self._family: list[LocalWorktree] = family if family is not None else []
        self._is_origin = family is None
        self._family.append(self)

    @classmethod
    async def create(
        cls, repo: str | os.PathLike[str], base: str = "HEAD", *, parent_dir: Optional[str] = None
    ) -> LocalWorktree:
        repo_path = str(Path(repo).resolve())
        _, out, _ = await _git(repo_path, "rev-parse", "--show-toplevel")
        toplevel = out.decode().strip()
        _, out, _ = await _git(toplevel, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common_dir = out.decode().strip()
        code, out, err = await _git(
            toplevel, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False
        )
        if code != 0:
            raise GitError(f"base ref {base!r} does not resolve to a commit in {toplevel}")
        return await cls._add(toplevel, common_dir, out.decode().strip(), parent_dir)

    @classmethod
    async def _add(
        cls,
        repo: str,
        common_dir: str,
        base_commit: str,
        parent_dir: Optional[str],
        family: Optional[list[LocalWorktree]] = None,
    ) -> LocalWorktree:
        group = Path(parent_dir) if parent_dir else _cache_root() / repo_key(repo, common_dir)
        group.mkdir(parents=True, exist_ok=True)
        root = str((group / uuid.uuid4().hex[:10]).resolve())
        lock = _repo_lock(common_dir)
        async with lock:
            try:
                await _git(repo, "worktree", "add", "--detach", "--quiet", root, base_commit)
            except BaseException:  # includes cancellation mid-checkout
                shutil.rmtree(root, ignore_errors=True)
                await _git(repo, "worktree", "prune", check=False)
                raise
        return cls(root, repo=repo, common_dir=common_dir, base_commit=base_commit, family=family)

    async def __aenter__(self) -> LocalWorktree:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def exec(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        stdin: Optional[str] = None,
    ) -> ExecResult:
        code, out, err = await run_process(
            argv,
            cwd=self.root,
            env=env,
            timeout=timeout,
            stdin=stdin.encode("utf-8", "surrogateescape") if stdin is not None else None,
        )
        return ExecResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def diff(self) -> str:
        """Everything since the base commit, including commits the agent made and new files.

        Uses a throwaway index inside this worktree's git dir, so the worktree's own index (what
        an agent sees in ``git status``) is left alone. ``.gitignore`` is honoured and untracked
        ``GENERATED_FILES`` are left out.
        """
        _, out, _ = await _git(self.root, "rev-parse", "--path-format=absolute", "--git-dir")
        index = os.path.join(out.decode().strip(), "multi-agents-diff-index")
        env = {"GIT_INDEX_FILE": index}
        try:
            await _git(self.root, "read-tree", self.base_commit, env=env)
            _, generated, _ = await _git(
                self.root,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                *(f"--exclude={pattern}" for pattern in GENERATED_FILES),
                env=env,
            )
            await _git(self.root, "add", "--all", env=env)
            if generated:
                await _git(self.root, "update-index", "--force-remove", "-z", "--stdin", env=env, stdin=generated)
            _, patch, _ = await _git(
                self.root,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                # Pin the prefixes: diff.noprefix / mnemonicPrefix / srcPrefix in the user's git
                # config would otherwise change the header and break `git apply`.
                "--src-prefix=a/",
                "--dst-prefix=b/",
                self.base_commit,
                env=env,
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(index)
        return patch.decode("utf-8", "surrogateescape")

    async def apply(self, patch: str) -> None:
        if not patch.strip():
            return
        await _git(
            self.root,
            "apply",
            "--binary",
            "--whitespace=nowarn",
            "-",
            stdin=patch.encode("utf-8", "surrogateescape"),
        )

    async def fork(self) -> LocalWorktree:
        return await self._add(
            self.repo,
            self.common_dir,
            self.base_commit,
            parent_dir=str(Path(self.root).parent),
            family=self._family,
        )

    async def close(self) -> None:
        if self._closed:
            return
        # `_closed` is set only once removal has finished, so a close interrupted by
        # cancellation can be completed by a later close (e.g. the origin's sweep below).
        if self._is_origin:
            for member in list(self._family):
                if member is not self:
                    await member.close()
        lock = _repo_lock(self.common_dir)
        async with lock:
            await _git(self.repo, "worktree", "remove", "--force", self.root, check=False)
            if os.path.exists(self.root):
                shutil.rmtree(self.root, ignore_errors=True)
            await _git(self.repo, "worktree", "prune", check=False)
        self._closed = True


async def close_to_completion(workspace: Workspace) -> None:
    """Close ``workspace`` fully even if this task is cancelled (again) meanwhile.

    Cancelling a team cancels its workers, and ``map_concurrently`` cancels them a second time while
    they are in their ``finally`` blocks; a plain ``await workspace.close()`` there would be
    interrupted half-way and leave the fork behind. Close errors are swallowed (cleanup is best
    effort); the cancellation is re-raised once the close has finished.
    """

    async def close() -> None:
        with contextlib.suppress(Exception):
            await workspace.close()

    closing = asyncio.ensure_future(close())
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


__all__ = [
    "ExecTimeout",
    "GitError",
    "LocalWorktree",
    "close_to_completion",
    "repo_key",
    "run_process",
    "user_cache_dir",
]

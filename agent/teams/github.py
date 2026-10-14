"""GitHub glue for ``multi-agents run``: take the task from an issue, publish the patch as a draft PR.

Everything goes through the ``gh`` CLI (authentication, API) and plain ``git``. Publishing never
touches the user's checkout: the patch is committed with a temporary index straight onto a new
branch ref at the base commit, pushed without force, and opened as a pull request.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .types import TeamResult, Usage
from .worktree import GitError, run_process

MAX_ISSUE_BODY_CHARS = 8000
BRANCH_PREFIX = "multi-agents/"
_GH_AUTH_EXIT = 4  # gh: "authentication required"
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


class GitHubError(RuntimeError):
    """A ``gh``/``git`` step failed; the message is meant for the user."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    url: str

    def record(self) -> dict[str, Any]:
        return {"number": self.number, "title": self.title, "url": self.url}


@dataclass(frozen=True)
class RepoInfo:
    """What ``gh`` knows about the repository the pull request will go to."""

    name_with_owner: str
    default_branch: str


@dataclass(frozen=True)
class Publication:
    branch: str
    commit: str
    url: Optional[str] = None

    def record(self) -> dict[str, Any]:
        return {"url": self.url, "branch": self.branch}


class PublishError(GitHubError):
    """Publishing failed after the work was safe locally; ``commands`` finish the job by hand."""

    def __init__(self, message: str, *, branch: Optional[str], commands: list[str]) -> None:
        super().__init__(message)
        self.branch = branch
        self.commands = commands


# --- running gh / git -------------------------------------------------------------------------


async def _gh(repo: str, *args: str) -> str:
    try:
        code, out, err = await run_process(["gh", *args], cwd=repo, env={"GH_PROMPT_DISABLED": "1"})
    except FileNotFoundError:
        raise GitHubError("the GitHub CLI (gh) is not installed; get it from https://cli.github.com") from None
    text = err.decode("utf-8", "replace").strip()
    if code != 0:
        if code == _GH_AUTH_EXIT or re.search(r"gh auth login|not logged in|GH_TOKEN", text, re.I):
            raise GitHubError("gh is not authenticated; run `gh auth login`")
        raise GitHubError(text or f"gh {' '.join(args[:2])} failed with exit code {code}")
    return out.decode("utf-8", "replace")


async def _git(repo: str, *args: str, env: Optional[dict[str, str]] = None, stdin: Optional[bytes] = None) -> str:
    code, out, err = await run_process(
        ["git", *args], cwd=repo, env={**_GIT_ENV, **(env or {})}, stdin=stdin
    )
    if code != 0:
        raise GitError(f"git {' '.join(args[:2])} failed ({code}): {err.decode('utf-8', 'replace').strip()}")
    return out.decode("utf-8", "surrogateescape")


async def _git_ok(repo: str, *args: str) -> bool:
    code, _, _ = await run_process(["git", *args], cwd=repo, env=_GIT_ENV)
    return code == 0


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


# --- issues -----------------------------------------------------------------------------------


async def read_issue(ref: str, repo: str) -> Issue:
    """Read an issue by number (against ``repo``'s default remote) or URL."""
    try:
        out = await _gh(repo, "issue", "view", ref, "--json", "number,title,body,url")
    except GitHubError as exc:
        if "not installed" in str(exc) or "not authenticated" in str(exc):
            raise
        raise GitHubError(f"cannot read issue {ref}: {exc}") from None
    try:
        data = _json_object(out)
        return Issue(int(data["number"]), str(data["title"]), str(data.get("body") or ""), str(data["url"]))
    except (ValueError, KeyError, TypeError):
        raise GitHubError(f"cannot read issue {ref}: unexpected output from gh") from None


def issue_instruction(issue: Issue, guidance: Optional[str] = None) -> str:
    """The task text for a team: the issue (body bounded), then any extra guidance from the user."""
    body = issue.body.strip()
    if len(body) > MAX_ISSUE_BODY_CHARS:
        body = body[:MAX_ISSUE_BODY_CHARS].rstrip() + "\n\n[issue text truncated]"
    parts = [
        f"Resolve this GitHub issue: #{issue.number} {issue.title}",
        issue.url,
        body or "(the issue has no description)",
    ]
    if guidance and guidance.strip():
        parts.append(f"Additional guidance:\n{guidance.strip()}")
    return "\n\n".join(parts)


# --- publishing -------------------------------------------------------------------------------


async def preflight(repo: str) -> RepoInfo:
    """Fail early (before any agent runs) if a PR could not be opened from ``repo``."""
    if shutil.which("gh") is None:
        raise GitHubError("the GitHub CLI (gh) is not installed; get it from https://cli.github.com")
    try:
        await _git(repo, "remote", "get-url", "origin")
    except GitError:
        raise GitHubError("--pr needs a GitHub remote named 'origin'; this repository has none") from None
    try:
        out = await _gh(repo, "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
        data = _json_object(out)
        return RepoInfo(str(data["nameWithOwner"]), str(data["defaultBranchRef"]["name"]))
    except (ValueError, KeyError, TypeError):
        raise GitHubError("cannot determine the GitHub repository for 'origin': unexpected output from gh") from None
    except GitHubError as exc:
        if "not authenticated" in str(exc):
            raise
        raise GitHubError(f"'origin' is not a GitHub repository gh can use: {exc}") from None


async def pr_base_branch(repo: str, base_ref: str, info: RepoInfo) -> str:
    """The branch the base commit came from if origin has it, else the default branch."""
    full = (await _git(repo, "rev-parse", "--symbolic-full-name", base_ref)).strip()
    branch = None
    if full.startswith("refs/heads/"):
        branch = full[len("refs/heads/") :]
    elif full.startswith("refs/remotes/origin/"):
        branch = full[len("refs/remotes/origin/") :]
    if branch and branch != "HEAD" and await _git_ok(repo, "ls-remote", "--exit-code", "--heads", "origin", branch):
        return branch
    return info.default_branch


def _slug(text: str, limit: int = 30) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "change"


async def _branch_taken(repo: str, branch: str) -> bool:
    if await _git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        return True
    # Best effort: if origin cannot be listed the push will fail (never forced) and say why.
    code, out, _ = await run_process(
        ["git", "ls-remote", "--heads", "origin", branch], cwd=repo, env=_GIT_ENV
    )
    return code == 0 and bool(out.strip())


async def _free_branch(repo: str, stem: str) -> str:
    branch, n = stem, 1
    while await _branch_taken(repo, branch):
        n += 1
        branch = f"{stem}-{n}"
    return branch


async def commit_patch(repo: str, base_commit: str, patch: str, branch: str, message: str) -> str:
    """Commit ``patch`` on top of ``base_commit`` as ``refs/heads/<branch>``; return the commit id.

    A temporary index is used, so the user's working tree, index and HEAD are not involved. The
    commit is made as the user's configured git identity. ``update-ref`` with an all-zero old
    value refuses to overwrite a branch that appeared in the meantime.
    """
    with tempfile.TemporaryDirectory(prefix="multi-agents-index-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        await _git(repo, "read-tree", base_commit, env=env)
        await _git(repo, "apply", "--cached", "--binary", env=env, stdin=patch.encode("utf-8", "surrogateescape"))
        tree = (await _git(repo, "write-tree", env=env)).strip()
    commit = (await _git(repo, "commit-tree", tree, "-p", base_commit, "-m", message)).strip()
    await _git(repo, "update-ref", f"refs/heads/{branch}", commit, "0" * 40)
    return commit


def _ref_text(issue: Issue, info: RepoInfo) -> str:
    m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/issues/\d+", issue.url)
    if m and m.group(1).lower() != info.name_with_owner.lower():
        return f"{m.group(1)}#{issue.number}"
    return f"#{issue.number}"


def _first_line(text: str, limit: int = 72) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _cost(usage: Usage) -> str:
    return "n/a" if usage.cost_usd is None else f"${usage.cost_usd:.4f}"


def render_pr_body(
    *,
    issue: Optional[Issue],
    info: RepoInfo,
    instruction: str,
    team: str,
    result: TeamResult,
    check: Optional[dict[str, Any]],
    files: list[str],
) -> str:
    lines: list[str] = []
    if issue:
        lines += [f"Closes {_ref_text(issue, info)}", f"Issue: {issue.url}", ""]
    lines += ["## Task", "", issue.title if issue else _first_line(instruction, 300), ""]
    lines += ["## Team", "", f"`{team}`", ""]
    if check:
        verdict = "passed" if check.get("passed") else f"failed (exit code {check.get('exit_code')})"
        duration = check.get("duration_s")
        took = f", {duration:.1f}s" if isinstance(duration, (int, float)) else ""
        lines += ["## Check", "", f"`{check.get('command')}`: **{verdict}**{took}", ""]
    lines += [
        "## Cost",
        "",
        "| role | harness@model | tokens in/cached/out | cost | secs |",
        "|---|---|---|---|---|",
    ]
    for run in result.runs:
        u = run.usage
        harness = f"{run.harness}@{run.model}" if run.model else run.harness
        lines.append(
            f"| {run.role} | {harness} | {u.input_tokens:,}/{u.cached_input_tokens:,}/{u.output_tokens:,} "
            f"| {_cost(u)} | {run.duration_s:.1f} |"
        )
    total = result.usage
    if total.cost_usd is None:
        known = sum(r.usage.cost_usd for r in result.runs if r.usage.cost_usd is not None)
        cost = f"${known:.4f} + {sum(1 for r in result.runs if r.usage.cost_usd is None)} unpriced run(s)"
    else:
        cost = _cost(total)
    lines += [
        "",
        f"**Total cost: {cost}** — "
        f"{total.input_tokens:,}/{total.cached_input_tokens:,}/{total.output_tokens:,} tokens, "
        f"{result.duration_s:.1f}s",
        "",
        f"## Files changed ({len(files)})",
        "",
        *files,
        "",
    ]
    return "\n".join(lines)


async def _files(repo: str, base_commit: str, commit: str) -> list[str]:
    out = await _git(repo, "diff", "--numstat", "--no-renames", "--no-color", base_commit, commit)
    files = []
    for line in out.splitlines():
        add, rem, name = line.split("\t", 2)
        files.append(f"- `{name}` (binary)" if add == "-" else f"- `{name}` +{add} -{rem}")
    return files


async def publish(
    repo: str,
    *,
    base_commit: str,
    base_ref: str,
    patch: str,
    instruction: str,
    team: str,
    result: TeamResult,
    issue: Optional[Issue],
    info: RepoInfo,
    check: Optional[dict[str, Any]],
    draft: bool,
    body_path: Path,
) -> Publication:
    """Commit ``patch`` to a new branch, push it to origin and open a pull request.

    The PR body is also written to ``body_path`` so a failed step can be finished by hand. Raises
    :class:`PublishError` (with the exact commands to run) when anything after the local commit
    fails; the local branch is kept.
    """
    title = _first_line(issue.title if issue else instruction)
    ident = str(issue.number) if issue else _slug(instruction)
    stem = f"{BRANCH_PREFIX}{ident}-{uuid.uuid4().hex[:6]}"
    message = f"{title}\n\nTeam: {team}\n" + (f"Issue: {issue.url}\n" if issue else "")
    try:
        branch = await _free_branch(repo, stem)
        commit = await commit_patch(repo, base_commit, patch, branch, message)
    except GitError as exc:
        raise PublishError(
            f"could not commit the patch to a branch: {exc}",
            branch=None,
            commands=[f"git -C {shlex.quote(repo)} checkout -b <branch> {base_commit[:12]} && git apply <patch>"],
        ) from None
    base = await pr_base_branch(repo, base_ref, info)
    body = render_pr_body(
        issue=issue,
        info=info,
        instruction=instruction,
        team=team,
        result=result,
        check=check,
        files=await _files(repo, base_commit, commit),
    )
    body_path.write_text(body, encoding="utf-8")
    push = f"git -C {shlex.quote(repo)} push origin {shlex.quote(branch)}"
    create = (
        f"gh pr create{' --draft' if draft else ''} --base {shlex.quote(base)} --head {shlex.quote(branch)} "
        f"--title {shlex.quote(title)} --body-file {shlex.quote(str(body_path))}"
    )
    try:
        # Never forced, and the branch name was verified free above.
        await _git(repo, "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    except GitError as exc:
        raise PublishError(
            f"could not push {branch} to origin: {exc}", branch=branch, commands=[push, create]
        ) from None
    try:
        out = await _gh(
            repo,
            "pr",
            "create",
            *(["--draft"] if draft else []),
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
        )
    except GitHubError as exc:
        raise PublishError(f"could not open the pull request: {exc}", branch=branch, commands=[create]) from None
    urls = re.findall(r"https?://\S+", out)
    if not urls:
        raise PublishError(
            f"gh did not report a pull request URL: {out.strip()!r}", branch=branch, commands=[create]
        )
    return Publication(branch=branch, commit=commit, url=urls[-1])


__all__ = [
    "GitHubError",
    "Issue",
    "Publication",
    "PublishError",
    "RepoInfo",
    "commit_patch",
    "issue_instruction",
    "preflight",
    "publish",
    "read_issue",
    "render_pr_body",
]

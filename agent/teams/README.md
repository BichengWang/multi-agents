# Teams of coding agents

Run one coding agent, a coder + reviewer loop, or several agents with a judge on the same task, each
worker in its own isolated git worktree, and get back a patch plus a full account of what it cost.

## Concepts

| Piece | What it is |
|---|---|
| `Workspace` | An isolated checkout at a base commit: `exec`, `diff`, `apply`, `fork`, `close`. `LocalWorktree` is the local implementation; the benchmark provides remote sandboxes. |
| `Harness` | One non-interactive coding-agent invocation: `claude` (Claude Code CLI), `codex` (Codex CLI) or `sdk` (built-in agent on the OpenAI Agents SDK, any model via LiteLLM). Harnesses reach the repository only through `Workspace.exec`. |
| `Team` | A topology that drives harnesses over a workspace and returns a `TeamResult`: the patch, every `HarnessRun` (roles `worker`, `coder`, `reviewer`, `judge`), duration and `meta`. |

Contracts are in [`types.py`](types.py). The loops are the [`agent.workflows`](../workflows/README.md)
patterns: `review` is a `RefineLoop`, `best` is a `BestOfN` with one generator per worker.

## Team specs

```text
solo:worker=H
review:coder=H,reviewer=H[,rounds=N]        # rounds defaults to 3
best:workers=H+H[+H...][,judge=H]           # judge defaults to the first worker's harness
H := harness[@model]                        # claude, codex, sdk
```

The kind is split off at the first `:`; model ids may contain `/`, `:`, `.` and `-`
(not `,` or `+`). Examples: `solo:worker=claude@haiku`, `solo:worker=codex@gpt-6-luna`,
`best:workers=claude@sonnet+codex+sdk@litellm/openrouter/some-model`. `Team.name` is the canonical
spec with every default spelled out (`rounds=3`, `judge=...`), so `build_team(team.name).name ==
team.name`. `build_team(spec)` and `build_harness(spec)` raise `SpecError` with a message naming the
bad part and the allowed values.

| Team | Behaviour |
|---|---|
| `solo` | The worker edits the given workspace. |
| `review` | The coder edits the workspace. After each attempt the reviewer judges the patch in a **fork with the patch applied** (it cannot touch the coder's workspace) and returns a verdict (`score` 0-10, `passed`, `feedback`). The coder resumes its session (where the harness supports it) with the feedback. Stops on `passed`, after `rounds` attempts, or when the coder stops changing anything. Returns the **best-scoring** patch (earliest on ties), not the last. |
| `best` | Every worker runs concurrently in its own fork, possibly with different harnesses. The judge scores each candidate patch independently (0-10) in another fork. The highest score wins, ties go to the earliest worker. A worker that crashes without producing a change is dropped; the run fails (empty patch) only if all do. |

A reviewer/judge that fails or answers with something that is not a verdict is recorded
(`meta["failures"]`, the run is kept) and scores 0; it never crashes the team. Verdicts use the CLI's
native schema flag where there is one (`claude --json-schema`, `codex exec --output-schema`) and
otherwise the last JSON object in the reply.

`TeamResult.meta` holds the canonical `team`, and per topology: `review` → `scores`, `rounds`,
`accepted`, `best_round`, `round_log`; `best` → `scores` (`null` for dropped workers), `chosen`
(1-based), `candidates` (harness, score and indices into `runs`). Failures go to `failures`.

## CLI

```bash
multi-agents run --team review:coder=codex,reviewer=claude@haiku,rounds=2 \
    --repo path/to/repo "Fix the failing test in tests/test_ops.py"
```

`multi-agents` is the installed console script; from a checkout, `python -m agent` is the same.

| Flag | Meaning |
|---|---|
| `--team SPEC` | required, see above |
| `--repo PATH` | repository (default: current directory); `--base REF` base ref (default `HEAD`) |
| `--timeout S` | seconds allowed per harness run (not for the whole team) |
| `--permissions safe\|bypass` | see Safety (default `safe`) |
| `--runs-dir DIR` | where records go (default `$XDG_CACHE_HOME/multi-agents/runs/<repo>-<hash>/`, i.e. never inside your repository; the path is printed) |
| `--keep-worktrees` | keep the main worktree for inspection (forks made by teams are always removed) |
| `--apply` | apply the patch to your tree; refused up front (before any agent runs) if the tree is not clean |

The run creates `<runs-dir>/<utc-timestamp>_<team>/record.json` (team, task, repo, base commit,
patch, every run with harness/role/model/tokens/cost/seconds/session/log, usage, total cost, duration,
meta) and `final.patch`, prints a Rich summary and the exact command to apply it:

```text
git -C <repo> apply <runs-dir>/<run>/final.patch
```

Your working tree is never modified unless you pass `--apply` (and then only if `git status` is
clean). The base is a commit: uncommitted changes in your tree are not visible to the workers.
Ctrl-C cancels all workers (their whole process groups are killed) and removes the worktrees.
Exit code: 0 patch produced, 1 empty patch, 2 usage error, 3 `--apply` refused or failed, 4 the team crashed, 130 interrupted.

### Where worktrees live

`LocalWorktree` creates detached `git worktree`s under `$MULTI_AGENTS_WORKTREE_DIR`, else
`$XDG_CACHE_HOME/multi-agents/worktrees`, else `~/.cache/multi-agents/worktrees`, grouped per repo.
Not inside your working tree (editors and `git status` never see them) and not inside `.git` (the
agent CLIs protect paths containing a `.git` segment). Creating one adds only metadata under the
repository's `worktrees/` directory; your index, HEAD and branches are untouched. `diff()` uses a
throwaway index, so it leaves the worktree's own index alone, includes new files and commits the
agent made, honours `.gitignore` and is binary-safe.

## Safety: permission modes

The adapters run real agents that edit files and run commands. Pick the mode deliberately.

* **`safe` (default).** `claude`: `--permission-mode acceptEdits` plus Claude Code's OS sandbox
  (`sandbox.enabled`, `autoAllowBashIfSandboxed`): edits and Bash (tests) are allowed automatically but
  Bash is confined to the workspace and has no network; anything that would still need approval is
  denied rather than waiting. If the sandbox cannot start on the host, Bash is denied (the agent can edit
  but not run tests). `codex`: `-s workspace-write` with `approval_policy="never"`.
* **`bypass`.** `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`.
  Use only where the workspace itself is the sandbox (the benchmark's containers). Claude Code
  refuses it as root unless `IS_SANDBOX=1` is in its environment: pass `env={"IS_SANDBOX": "1"}`
  to `build_team`/`build_harness` (or set it in the sandbox).
* **`sdk` has no permission system.** Its `run_shell` tool runs with the workspace's own privileges,
  unconfined. Locally that means your user account; the CLI prints a warning. Model calls are made from
  the host process (keys from the host environment), tool commands in the workspace.

Your own CLI configuration still applies to `claude` and `codex` (user settings, `CLAUDE.md`/`AGENTS.md`,
hooks, default model).

## Cost semantics

`Usage.cost_usd` is `None` when a run is unpriced, and any sum containing an unpriced run is `None`
(`TeamResult.usage`), so totals never under-report silently. The CLI then shows the priced part plus
"N unpriced run(s)". `input_tokens` includes cached tokens; `cached_input_tokens` is the cache-read part.

| Harness | Tokens | Cost |
|---|---|---|
| `claude` | from `modelUsage` (input + cache read + cache creation, output); on resume the CLI's totals are cumulative over the session, so the previous total is subtracted | the CLI's own `total_cost_usd` (same delta); `None` if absent or if the session's earlier total was not seen by this harness object |
| `codex` | `turn.completed` usage, minus the previous total when resuming (the CLI reports a cumulative thread total) | [`pricing.py`](pricing.py): list price × tokens, short-context rates, for models in the table. The stream does not name the model: with no `@model` it is read from the session's rollout file; if that fails, or the model is not listed, `None` |
| `sdk` | summed over all requests of the run | `pricing.py` for OpenAI models; `litellm.cost_per_token` for `litellm/...` models when litellm knows the model; else `None` |

Prices are never guessed or borrowed from a similar model. `pricing.py` records its source URL and date;
batch/flex/fast tiers and data-residency uplifts are not applied. `is_priced("codex@model")` tells a
caller up front whether a harness spec will report a cost.

## Python API

```python
from agent.teams import CodingTask, LocalWorktree, build_team

team = build_team("best:workers=claude@haiku+codex", permissions="safe", timeout=900)
async with await LocalWorktree.create("path/to/repo") as ws:
    result = await team.solve(CodingTask("t1", "Fix the failing test"), ws)
print(result.patch, result.usage.cost_usd, [(r.role, r.harness) for r in result.runs])
```

## Issue to pull request

```bash
multi-agents run --repo . --team "solo:worker=claude@haiku" --issue 42 --pr
multi-agents run --team "review:coder=codex,reviewer=claude" --issue https://github.com/OWNER/REPO/issues/42 \
    --pr --ready "keep the public API unchanged"
```

Both need the [GitHub CLI](https://cli.github.com) (`gh`), logged in.

* `--issue N|URL` reads the issue (`gh issue view`, run in the target repo, so a bare number resolves
  against its default remote) and uses its title and body (capped at 8000 characters) as the task. A
  positional instruction is added as extra guidance. The issue is stored in `record.json` as
  `issue: {number, title, url}`. A missing `gh`, no login, or an unknown issue stops the run with exit
  code 2 before any agent starts.
* `--pr` publishes a non-empty patch: a branch `multi-agents/<issue-number-or-slug>-<run-id>` is
  committed at the base commit with a temporary index (your checkout, index and current branch are never
  touched), as your configured git identity, pushed to `origin` (never forced; if the name exists locally
  or on `origin`, a `-2`, `-3`... suffix is used), and opened with `gh pr create --draft` against the
  branch the base commit came from if `origin` has it, else the repository's default branch. `--ready`
  opens a regular PR instead. Before the team runs, `--pr` checks that `gh` is logged in and that `origin`
  is a repository `gh` can use (exit code 2 otherwise).
* The PR body has `Closes #N` (`Closes OWNER/REPO#N` for an issue in another repository), the team spec,
  the check result when `--check` was used, a per-role cost/tokens/time table with the total, and the
  files changed. It is also saved as `pr-body.md` next to `record.json`. The PR URL and branch are stored
  as `pull_request: {url, branch}` and printed.
* An empty patch opens nothing (exit code 1). If committing, pushing or `gh pr create` fails (exit code
  3), the local branch is kept and the exact commands to finish by hand are printed.

`gh` picks the repository for `pr create` from your remotes (`gh repo set-default` to change it). Fork
workflows where `origin` is not that repository are not handled specially.

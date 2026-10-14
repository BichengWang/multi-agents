# Teams of coding agents

Run one coding agent, a coder + reviewer loop, or several agents with a judge on the same task, each
worker in its own isolated git worktree, and get back a patch plus a full account of what it cost.

## Concepts

| Piece | What it is |
|---|---|
| `Workspace` | An isolated checkout at a base commit: `exec`, `diff`, `apply`, `fork`, `close`. `LocalWorktree` is the local implementation; the benchmark provides remote sandboxes. |
| `Harness` | One non-interactive coding-agent invocation: `claude` (Claude Code CLI), `codex` (Codex CLI) or `sdk` (built-in agent on the OpenAI Agents SDK, any model via LiteLLM). Harnesses reach the repository only through `Workspace.exec`. |
| `Team` | A topology that drives harnesses over a workspace and returns a `TeamResult`: the patch, every `HarnessRun` (roles `worker`, `coder`, `reviewer`, `judge`, `planner`, `integrator`), duration and `meta`. |

Contracts are in [`types.py`](types.py). The loops are the [`agent.workflows`](../workflows/README.md)
patterns: `review` is a `RefineLoop`, `best` is a `BestOfN` with one generator per worker.

## Team specs

```text
solo:worker=H
review:coder=H,reviewer=H[,rounds=N]        # rounds defaults to 3
best:workers=H+H[+H...][,judge=H]           # judge defaults to the first worker's harness
plan:planner=H,workers=H[+H...][,integrator=H][,max_tasks=N]
                                            # integrator defaults to the first worker's harness; max_tasks to 4
H := harness[@model]                        # claude, codex, sdk
                                            # reviewer= and judge= also accept `check`, see Checks
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
| `plan` | A **planner** (it runs in a fork that is discarded, so anything it writes is thrown away) splits the task into 1..`max_tasks` independent subtasks `{title, instruction, paths?}`, returned as JSON (native schema flag where the CLI has one, else the last JSON object in the reply). Workers solve the subtasks concurrently, each in its own fork (subtask *i* goes to `workers[i % len(workers)]`; all at once unless `PlanTeam(max_concurrency=N)` bounds it; the prompt has the whole task, the subtask and the titles of the others, and tells the worker to stay out of their scope). The patches are merged into the team's workspace in subtask order with `git apply --3way` and left as uncommitted, unstaged changes like any other team's; a patch that conflicts or does not apply is rolled back (the clean ones stay) and the **integrator** runs in the workspace with the task, all patches and the failure to produce the combined change. A subtask whose worker failed (or changed nothing) is recorded in `failures` and the integrator is told which subtask is missing and asked to complete it. With a `--check` that fails on the integrated patch, the integrator gets one fix pass with the check output and the check runs again. If the planner fails or returns an invalid plan (recorded in `failures`), or the plan has one subtask, the team behaves like `solo` with the first worker (`meta["fallback"]`). |

A reviewer/judge that fails or answers with something that is not a verdict is recorded
(`meta["failures"]`, the run is kept) and scores 0; it never crashes the team. Verdicts use the CLI's
native schema flag where there is one (`claude --json-schema`, `codex exec --output-schema`) and
otherwise the last JSON object in the reply.

`TeamResult.meta` holds the canonical `team`, and per topology: `review` → `scores`, `rounds`,
`accepted`, `best_round`, `round_log`; `best` → `scores` (`null` for dropped workers), `chosen`
(1-based), `candidates` (harness, score and indices into `runs`). Failures go to `failures`.

`plan` → `subtasks` (title, instruction, paths, `worker`, `status` of `applied`/`conflict`/`failed`/`empty`, indices into `runs`),
`integration` (`applied`/`conflicts`/`missing` subtask numbers, `integrator_runs`), `check_before_fix`
(the failing check result, when a fix pass ran) and `fallback`. Its `runs` have roles `planner`,
`worker` and `integrator`, in that order.

## Checks

`--check CMD` (`build_team(spec, check=CMD)`) is an objective verdict from a shell command: tests, a
build, a linter. After the team finishes, `sh -c CMD` runs in a **fresh fork with the final patch
applied**. It gets your environment (including `PATH`) but not your working tree: the fork starts at
the base commit, so untracked dependencies (`node_modules`, a virtualenv) are missing, and **a
virtualenv on your `PATH` still points at your original checkout**: an editable install
(`pip install -e .`) imports *that* tree, not the patched fork, and the check would silently judge the
wrong code. Make the command self-contained and install into the fork, e.g.
`--check 'python -m venv .venv && .venv/bin/pip install -q -e . pytest && .venv/bin/pytest -q'`.

The command is bounded by the team's `--timeout` (a timeout is a failed check); **without
`--timeout` it is unbounded**. The result is recorded in `TeamResult.meta["check"]` and as `check`
in `record.json`: `{command, passed, exit_code, output_tail, duration_s}` (`exit_code` is `null` if
the command did not finish or there was no patch to check; `output_tail` is the end of stdout then
stderr). The CLI summary prints pass/fail; the check does not change the exit code of
`multi-agents run`. (It can change which patch a team returns, see below.)

**The check runs on what the agents left behind, including edits to the tests.** An agent that
rewrites or deletes the failing test, `conftest.py` or the `Makefile` makes the check pass, and the
prompts only ask it not to. The fork's `HEAD` is the base commit, so restore the files the agents
must not touch before running: `--check 'git checkout HEAD -- tests && python3 -m unittest -q'`
(this does not restore files the agents *added*, e.g. a new `conftest.py`; add `git clean -fdq tests`).
Read the patch before you apply it either way.

It also changes how teams decide:

| Use | Behaviour |
|---|---|
| `reviewer=check` | A test-driven `review` loop with no LLM reviewer: exit 0 scores 10 and passes (the loop stops), anything else scores 0 and the coder gets the tail of the output as feedback. |
| `judge=check` | `best` keeps the worker whose patch passes the check (ties go to the earliest worker). |
| LLM `reviewer` + `--check` | The reviewer's prompt includes the check result for the patch under review. |
| LLM `judge` + `--check` | Every candidate is checked as well. Candidates that fail rank below every candidate that passes; the judge's scores order within each group. `meta["candidates"][i]["check"]` has each result; `meta["scores"]` stays the judge's raw scores. |

`check` as `reviewer=`/`judge=` implies `--check` with the same command and raises `SpecError`
without one; as `worker`, `coder`, `planner` or in `workers` it is rejected. The check reviewer/judge
and the final recorded check share one memoized run per patch (the command is not run twice on the
same patch). A check is not a harness run: it consumes no tokens. As `reviewer=check` or
`judge=check` it appears in `runs` with role `reviewer`/`judge`, harness `check` and cost exactly $0.00.
With `reviewer=check` all failing attempts score 0, so the team returns the **last** attempt (the one
with the most feedback) rather than the first.

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
| `--check CMD` | shell command run against the final patch in a fork; see Checks |

The run creates `<runs-dir>/<utc-timestamp>_<team>/record.json` (team, task, repo, base commit,
patch, every run with harness/role/model/tokens/cost/seconds/session/log, usage, total cost, duration,
meta) and `final.patch`, prints a Rich summary and the exact command to apply it:

```text
git -C <repo> apply <runs-dir>/<run>/final.patch
```

Your working tree is never modified unless you pass `--apply` (and then only if `git status` is
clean). The base is a commit: uncommitted changes in your tree are not visible to the workers.
Exit code: 0 patch produced, 1 empty patch, 2 usage error, 3 `--apply` refused or failed (or `--pr` delivery failed), 4 the team crashed, 130/143/129 cancelled by SIGINT/SIGTERM/SIGHUP.
SIGINT, SIGTERM and SIGHUP all take the same path: the team is cancelled, every worker's process
group is killed and the worktrees are removed (so a caller's tool timeout, which sends SIGTERM, does
not leave agents running and billing); a second signal force-quits.

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

**Issue text is untrusted input.** Whoever can open an issue on the repository writes part of the
agent's prompt. The text is passed to the agent as a delimited problem description it is told not to
obey as instructions, which reduces but does not remove prompt-injection risk. Do not combine `--issue`
on a repository where strangers can open issues with `--permissions bypass` or the `sdk` harness (both
run commands with your privileges), and read the patch and the draft PR before trusting them.

* `--issue N|URL` reads the issue (`gh issue view`, run in the target repo, so a bare number resolves
  against its default remote) and uses its title and body (capped at 8000 characters) as the task. A
  positional instruction is added as extra guidance. The issue is stored in `record.json` as
  `issue: {number, title, url}`. A missing `gh`, no login, or an unknown issue stops the run with exit
  code 2 before any agent starts.
* `--pr` publishes a non-empty patch: a branch `multi-agents/<issue-number-or-slug>-<run-id>` is
  committed at the base commit with a temporary index (your checkout, index and current branch are never
  touched), as your configured git identity (unsigned, even with `commit.gpgsign`: sign it afterwards
  if a ruleset requires signatures), pushed to `origin` (never forced; if the name exists locally or on
  `origin`, a `-2`, `-3`... suffix is used), and opened with `gh pr create --draft` against the branch
  the base commit came from if `origin` has it, else the repository's default branch. `--ready` opens a
  regular PR instead. The push runs your repository's `pre-push` hook, if any, like any `git push`.
* Before the team runs, `--pr` checks (exit code 2 otherwise) that `gh` is logged in, that `origin` is a
  repository `gh` can use, and that the base commit is already on that base branch on `origin`. The
  pushed branch carries every commit below it, so unpushed local commits would otherwise be published
  and show up in the PR: push them, or pass `--base origin/<branch>`.
* The PR body has `Closes #N` (`Closes OWNER/REPO#N` for an issue in another repository), the team spec,
  the check result when the run record has one, a per-role cost/tokens/time table with the total, and
  the files changed. It is also saved as `pr-body.md` next to `record.json`. The PR URL and branch are
  stored as `pull_request: {url, branch}` and printed.
* An empty patch opens nothing (exit code 1). If committing, pushing or `gh pr create` fails (exit code
  3, the same code as a failed `--apply`), the local branch is kept where there is one and the exact
  commands to finish by hand are printed; the patch is always saved as `final.patch` next to the record.

`gh` picks the repository for `pr create` from your remotes (`gh repo set-default` to change it). Fork
workflows where `origin` is not that repository are not handled specially.

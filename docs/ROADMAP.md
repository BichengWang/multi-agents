# Roadmap

## Thesis

Dozens of tools run several coding agents in parallel; none of them tells you whether the team
beat a single agent, or what the extra agents cost. Published evidence points both ways: a
coordinator-plus-reviewer team (Agyn, arXiv 2602.01465) reached 72.2% on SWE-bench Verified, +7.2
over mini-SWE-agent but only +0.4 over the strongest single-agent baseline; a decentralized patch
editor plus validator (icat-agent, arXiv 2606.25514) gained +3.6 / +8.4 over mini-SWE-agent; and
loosely coupled teams lost 2–15 points in a 260-configuration study. Numbers and caveats:
[agent-company-survey.md](agent-company-survey.md). This project is a headless runner
for teams of coding agents that ships with the measurement: every team topology is benchmarked
against solo agents on fresh, uncontaminated tasks, with resolve rate, cost and wall time.

It runs on its own or inside terminal/desktop orchestrators; it does not compete with them on UI.

## Architecture

```mermaid
flowchart LR
  CLI["CLI / Python API"] --> Team
  Bench["Benchmark runner"] --> Team
  Team["Team topology\nsolo | review | best | plan"] --> H["Harness\nexternal CLI or SDK agent"]
  Team --> W["Workspace"]
  H --> W
  W --> WT["Local git worktree"]
  W --> SB["Remote sandbox (benchmark)"]
  Team --> R["TeamResult\npatch + runs + usage"]
```

Contracts live in [`agent/teams/types.py`](../agent/teams/types.py). Harnesses and teams reach the
repository only through `Workspace.exec`, so the same team code runs locally and in benchmark
sandboxes. Teams reuse the patterns in `agent/workflows` (`RefineLoop`, `BestOfN`).

### Team specs

Teams are selected with a compact spec string; the canonical form is the team's `name`.

```text
solo:worker=H
review:coder=H,reviewer=H[,rounds=N]          # rounds defaults to 3
best:workers=H+H[+H...][,judge=H]             # judge defaults to the first worker's harness
plan:planner=H,workers=H[+H...][,integrator=H][,max_tasks=N][,replans=N][,reviewer=H][,rounds=N]
                                              # integrator defaults to the first worker's harness; max_tasks to 4;
                                              # replans to 1 (0 = never); rounds (default 3) needs reviewer
H := harness[@model]                          # claude, codex@gpt-5-codex, sdk@litellm/<provider>/<model>
                                              # check: the --check command as reviewer or judge
```

## Milestones

1. **Pivot** (done) — contracts; non-coding workflows and `ml/` removed (kept in history at the
   `pre-pivot` tag).
2. **Team runner** (done) — harness adapters for external coding CLIs and a built-in any-model SDK
   agent; local worktree workspaces; `solo`, `review` and `best` teams; CLI; run records with cost.
3. **Benchmark** (done) — fresh-task dataset filtered by creation date; remote Modal sandboxes
   (also as a detached Modal run) or a local Docker daemon (`--backend docker`); hidden-test
   evaluation; report with resolve rate, cost, wall time and confidence intervals. Live on local
   Docker since 2026-10-28 and on Modal since 2026-10-29.
4. **Pilot results** (done for one model tier) — 48 fresh tasks × solo, review and best of 3 on
   Claude Haiku 5.5; table, analysis and raw trajectories [published](pilot-2026-10.md). The teams
   resolved one or two more tasks than solo, not significant, at 1.6× and 2.7× the cost. Next: a
   frontier tier, and `--check` as reviewer/judge (current stage, below).
5. **Launch** — README led by the results, terminal demo, launch posts.
6. **Growth loop** — monthly refresh on newly created tasks, more harnesses, community-submitted
   team topologies.

## Current stage: second pilot (objective checks, frontier tier)

The Haiku pilot's teams failed in their judgment: the reviewer approved 47 of 48 final patches,
19 of them wrong, and the judge tied on 32 of 48 tasks. This stage tests whether an objective check
fixes that, and whether a stronger model changes the picture.

1. **The task's test suite as `check` in the benchmark** (code done, tested offline) —
   `reviewer=check` / `judge=check` in a benchmark team runs the task's visible test suite, without
   the hidden tests, in a clean sandbox and passes a patch iff nothing fails that passed at the base
   commit ([benchmark guide](../agent/bench/README.md#methodology)).
2. **Live smoke test** — one task on Modal with `review:coder=claude@claude-haiku-5-5,reviewer=check`
   and `best:...,judge=check`: checks that each task's parser reads its own suite at the base commit
   and that the timings match the estimate (cents of model cost, about an hour of sandbox time).
3. **Check teams on the pilot's 48 tasks (Haiku 5.5)** — `review` with `reviewer=check` and `best` of
   3 with `judge=check`, compared with the published solo baseline on the same tasks. Model cost is
   about the coder's alone ($0.06 to $0.18 per task, under $15 in all); sandbox time dominates.
   Re-run solo too if Claude Code's version has changed since the pilot (2.1.310).
4. **Frontier tier** — the same 48 tasks with Claude Sonnet 5.5: solo, review and best of 3, plus the
   better check team. Sonnet 5.5's list price is 4× to 20× Haiku 5.5's (by Haiku's prompt tier),
   so the Haiku pilot's $15.67 scales to about $63 to $313 for the three LLM teams; measure 5 tasks
   first (`--limit 5`) and set `--max-cost-usd` from that.
5. **Publish** — a results page like [pilot-2026-10.md](pilot-2026-10.md), then milestone 5.

Steps 2 to 4 spend money on the Anthropic API and Modal, so each one starts only after sign-off.

## Past stage: teams that finish real work (done)

While the pilot (milestone 4) waited on API credentials, the runner gained end-to-end work
features, all merged and smoke-tested with real CLIs:

1. **Objective checks** — `--check CMD` runs a command (tests, build, lint) against the final
   patch in a fork and records pass/fail. `check` can be the reviewer (a test-driven loop with no
   LLM reviewer) or the judge; with an LLM judge, candidates that fail the check rank below those
   that pass.
2. **`plan` topology** — a planner splits the task into independent subtasks, workers solve them
   in parallel forks, an integrator merges the patches and resolves conflicts, and the check
   verifies the result. This is the coordinator pattern the published gains come from.
3. **Use from inside your agent** — a Claude Code plugin and a Codex plugin, installable from this
   repository, that let a running agent hand a task to a team and review the patch.
4. **Issue to pull request** — `--issue N|URL` takes the task from a GitHub issue; `--pr` commits the
   result to a new branch and opens a draft pull request with the run summary.

## Past stage: agent-company governance (done)

[agent-company-survey.md](agent-company-survey.md) compares the most-starred multi-agent
"company" projects with this runner and lists three gaps. Each is closed in code, tested offline and
smoke-tested with real CLIs.

1. **Board: run budget hard stop** — `--max-cost USD [--allow-unpriced]`. Every harness except
   `check` is wrapped in a shared budget: no new agent run starts once the limit is spent, and a
   warning is printed at 80%, as in Paperclip and MetaGPT. In-flight runs finish, so spend can
   overshoot by their cost; an unpriced run blocks further runs unless `--allow-unpriced`, and the
   CLI rejects unpriced harnesses up front. The record carries the spend and refused runs.
2. **Executive: replan failed subtasks** — `plan` `replans=N` (default 1). Subtasks whose worker
   failed or changed nothing go back to the planner, which sees the merged state and returns new
   subtasks for the rest; rotated workers solve them. This is the Magentic-One stall/replan and
   CAMEL failure-policy pattern. Conflicts still go to the integrator.
3. **Team lead: review each subtask** — `plan` `reviewer=H[,rounds=N]`. Every subtask is a
   coder ⇄ reviewer loop in its own worktree and its best patch is merged. CAID reports a manager
   reviewing every round at 60.2% against 55.1% when engineers verified their own work.

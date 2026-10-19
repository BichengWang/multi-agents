---
name: multi-agent-skill
description: Build multi-agent teams for real work in this repo. Use when adding a team topology, a harness, or a workspace under agent/teams, or when extending the shared patterns in agent/workflows.
---

# multi-agent-skill

This repo runs teams of coding agents and measures whether a team beats a solo agent.

## core logic view

```
CodingTask ──► Team (topology) ──► TeamResult(patch, runs, usage, duration_s, meta)
                 │
                 ├─ drives Harness   : one coding agent run to completion (external CLI or SDK agent)
                 └─ in a Workspace   : isolated checkout at the base commit (local worktree or sandbox)

Topologies reuse agent/workflows:
  SequentialWorkflow : A → B → C
  RefineLoop         : generate ⇄ evaluate (feedback) until accepted; returns the best draft
  BestOfN            : N × (generate → evaluate) concurrently, keep the best
  Router             : classify → hand off to one specialist workflow
```

Contracts: `agent/teams/types.py` (see `reference/protocol.md`). Harnesses and teams touch the
repository only through `Workspace.exec`, so one team runs locally and in benchmark sandboxes.

## rules

1. Never change the contracts in `agent/teams/types.py` without updating every implementation.
2. Patterns in `agent/workflows` never call the SDK directly; they call an injected
   `runner(agent, input_text) -> final_output`. Tests inject a fake runner.
3. Evaluators used by `RefineLoop` / `BestOfN` return an `agent.workflows.Verdict` subclass
   (`score`, `passed`, `feedback`, plus domain fields).
4. Cost honesty: a harness that cannot report or price a call returns `Usage.cost_usd=None`;
   never guess prices. Every harness call made by a team appears in `TeamResult.runs`.
5. Reviewers and judges must not mutate a worker's workspace; give them a fork.
6. The run budget (`--max-cost`, `agent/teams/budget.py`) is enforced by `build_team`, which wraps every
   harness except the `check` pseudo-harness in `BudgetedHarness`. Topologies never call the budget:
   a refused run raises `BudgetExceeded`, which they record like any failed run (`meta["failures"]`,
   not in `TeamResult.runs`). Runs already admitted finish, so spend can overshoot; unpriced runs
   block further runs unless `allow_unpriced`.
7. `plan` runs may carry the roles `coder`/`reviewer` inside subtasks (`reviewer=H`, a `ReviewTeam`
   per subtask in its own fork) and extra `planner` runs (`replans=N`); subtasks are numbered across
   rounds and replaced ones get status `replanned`.

## adding a harness

1. `agent/teams/harnesses/<name>.py`: implement `Harness.run` with `workspace.exec` only; parse
   the CLI's machine-readable output into `HarnessRun` (final message, session id, usage, cost).
   If the CLI reports cumulative usage on resume, subtract the session's previous total.
2. Register it in `agent/teams/spec.py` (`HARNESSES`, `build_harness`, `is_priced`).
3. Map `--permissions safe|bypass` to the CLI's own sandbox / bypass flags.
4. Capture real CLI output as sanitized fixtures in `tests/teams/fixtures/` and test parsing.

## adding a team topology

1. Implement it in `agent/teams/topologies.py` (or its own module, like `plan.py`) on top of an
   `agent/workflows` pattern.
2. Add its spec kind and keys to `agent/teams/spec.py`; `Team.name` must be the canonical spec.
3. Test with fake harnesses on a temp git repo (`tests/teams/`): selection, failures, cleanup.

## tests

Offline and deterministic: `tests/workflows/fakes.py` (`FakeAgent`, `fake_runner`,
`scripted_evaluator`) for patterns; fake harnesses and temp git repos for teams. Run
`make test`, then `make check` before a PR.

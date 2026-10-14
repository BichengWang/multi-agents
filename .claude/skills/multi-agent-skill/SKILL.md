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

## tests

Offline and deterministic: `tests/workflows/fakes.py` (`FakeAgent`, `fake_runner`,
`scripted_evaluator`) for patterns; fake harnesses and temp git repos for teams. Run
`make test`, then `make check` before a PR.

# Protocol

## Teams (`agent/teams/types.py`)

| type | contract |
|---|---|
| `Workspace` | `root`; async `exec(argv, env, timeout, stdin) -> ExecResult`, `diff()`, `apply(patch)`, `fork()` (new workspace at the base commit), `close()` |
| `Harness` | `name`, `model`; async `run(workspace, prompt, role, session_id, timeout) -> HarnessRun` |
| `HarnessRun` | `harness`, `model`, `role`, `final_message`, `exit_code`, `usage`, `duration_s`, `session_id`, `log` |
| `Usage` | token counts and `cost_usd`; `None` means unpriced and makes any sum unpriced |
| `CodingTask` | `task_id`, `instruction` |
| `Team` | `name` (canonical team spec); async `solve(task, workspace) -> TeamResult` |
| `TeamResult` | `patch`, `runs` (every harness call, in order), `duration_s`, `meta`, `usage` |

Team spec grammar: see `docs/ROADMAP.md#team-specs`.

Run budget (`agent/teams/budget.py`, not part of the contracts): `Budget(max_cost_usd, allow_unpriced=False)`
with `admit(harness, role)` (raises `BudgetExceeded` once the limit is spent, or after an unpriced run
unless allowed), `charge(run)` and `to_dict()`; `BudgetedHarness` wraps a `Harness` and forwards its
`run(...)` keywords. `build_team(spec, budget=...)` wraps every harness except `check`. `TeamResult.meta`
is unchanged; the CLI adds `budget` to `record.json`.

`plan` roles in `TeamResult.runs`, in order: `planner`, the first wave's `worker` runs (`coder`/`reviewer`
with a reviewer), then per replan round `planner` and its workers, then `integrator`. `meta`: `subtasks`
(each with `round` and `status` `applied`/`conflict`/`failed`/`empty`/`replanned`), `replans`,
`integration`, `fallback`.

## Workflow patterns (`agent/workflows`)

```python
AgentRunner = Callable[[Any, str], Awaitable[Any]]

class Verdict(BaseModel):
    score: float     # 1-10 overall
    passed: bool     # ready to ship as-is
    feedback: str    # actionable revision notes
```

`RefineLoop` accepts a draft when `score >= threshold` (if a threshold is set) else when `passed`,
and raises `TypeError` if the evaluator output lacks these fields.

| `WorkflowResult` field | meaning |
|---|---|
| `final_output` | chosen output (last stage for sequential; best draft for refine / best-of-n) |
| `verdict` | the evaluator verdict for `final_output` (refine / best-of-n) |
| `meta` | refine: `iterations`, `accepted`, `scores`; best-of-n: `chosen` (1-based), `scores` |
| `steps` | `StepRecord(name, agent, input, output, iteration)` for every agent call |

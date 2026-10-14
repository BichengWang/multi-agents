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

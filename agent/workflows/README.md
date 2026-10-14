# Workflow Patterns

Reusable orchestration patterns for multi-agent systems. Each pattern takes plain agents plus an
injectable `runner` (`async (agent, input_text) -> final_output`). The default runner uses the
OpenAI Agents SDK (`Runner.run`); tests pass a fake runner so everything runs offline.

| Pattern | Shape | Use when |
|---|---|---|
| `SequentialWorkflow` | A → B → C | Each stage transforms the previous output (plan → write → review). |
| `RefineLoop` | generate ⇄ evaluate | Quality matters and the evaluator's feedback can drive revisions. |
| `BestOfN` | N × (generate → evaluate) in parallel, keep max | You want diversity; revisions are less useful than fresh attempts. |
| `Router` | classify → one of N workflows | One entrypoint serves requests that need different specialists. |

Every run returns a `WorkflowResult` with `final_output`, the `verdict` that chose it (refine /
best-of-n), `meta` (iterations, scores, chosen candidate), and `steps` — a full trace of every
agent call with its input and output.

## Execution boundaries

Every agent call goes through the supplied `AgentRunner`, so `RunTracker` captures nested calls
as well as top-level stages. Workflows do not start their own SDK trace or bypass the runner
with streamed SDK calls; the caller owns any enclosing trace.

`BestOfN(generator=[...])` accepts one generator per candidate (`n` defaults to the list length) for
candidates that differ in kind, e.g. different harnesses, not only in prompt.

`BestOfN` uses `map_concurrently(items, worker, max_concurrency=...)`. It uses a fixed-size
worker pool, returns results in input order, and cancels and awaits all workers on failure or
caller cancellation. Queued work does not start after a worker fails. `BestOfN` defaults to
three concurrent workers; configure `BestOfN(max_concurrency=...)` to change the limit. A worker
owns a complete generate → evaluate candidate, and equal scores select the earliest candidate.
Ordinary `BestOfN` candidate failures are collected rather than propagated: failed candidates have
`None` in `meta["scores"]` and an entry in `meta["failures"]`; successful candidates remain eligible.
The run raises if all candidates fail. Cancellation is never treated as a candidate score.

## Evaluators

`RefineLoop` and `BestOfN` need an evaluator with structured output exposing `score`, `passed`,
and `feedback`. Subclass `Verdict` to add domain-specific fields:

```python
from agents import Agent
from agent.workflows import Verdict

class PatchReview(Verdict):
    tests_pass: bool

evaluator = Agent(name="Reviewer", instructions="...", output_type=PatchReview)
```

## Router

`Router` asks a classifier agent (`output_type=RouteDecision`: `route`, `confidence`, `reason`)
to pick one `Route(name, description, handler)`; the handler is any `async (query) -> output`,
usually another workflow's `run`. Unknown routes or confidence below `min_confidence` go to
`fallback` (or raise if none is set), and `run(query, force="name")` skips classification. When
the handler returns a `WorkflowResult`, its steps are appended after the `classify` step.

```python
router = Router(classifier, [Route("bugfix", "fix a reported defect", bugfix_wf.run), ...], fallback="bugfix")
result = await router.run("The CSV export drops the last row")
result.meta  # {'route': 'bugfix', 'confidence': 0.93, 'fallback_used': False, ...}
```

## Example

```python
from agent.workflows import RefineLoop

result = await RefineLoop(generator, evaluator, max_iterations=3, threshold=8).run("Draft the migration plan")
print(result.final_output, result.meta)  # {'iterations': 2, 'accepted': True, 'scores': [6.0, 8.5]}
```

`RefineLoop` returns the best-scoring draft, not the last one, so a regression in a late revision
never replaces a better earlier draft.

## Refinement checkpoints

Supply an async `checkpoint` callback to inspect an evaluated draft before another iteration.
It receives a `RefineCheckpoint` containing the original query, iteration, draft and verdict, and
returns a `CheckpointDecision`. Returning `continue_refining=False` stops with the best-scoring
draft so far; otherwise optional feedback is appended to the revision prompt, alongside the
evaluator's feedback. The callback is skipped once a draft is accepted or the iteration limit is
reached. Leaving it unset preserves automatic refinement.

```python
from agent.workflows import CheckpointDecision, RefineLoop

async def review(state):
    return CheckpointDecision(feedback="Keep the public API unchanged.")

result = await RefineLoop(generator, evaluator, checkpoint=review).run("Draft the migration plan")
```

`cli_checkpoint` is a ready-to-use callback: Enter continues, text steers, `/stop` or EOF stops.
Each decision is recorded as a `checkpoint` step, and `meta["checkpoint_stopped"]` distinguishes
a human stop from normal completion. Stopping does not mark a rejected draft as evaluator-accepted.

## Run artifacts

Wrap the runner in a `RunTracker` to time every agent call. With no base runner it calls the SDK
itself, adds a tracing span per call and records token usage. `run_record()` + `save_run()` then
write the result, verdict, meta, step trace, per-call timings and token totals to `runs/`:

```python
from agent.workflows import RefineLoop, RunTracker, run_record, save_run

tracker = RunTracker()                      # or RunTracker(base=fake_runner) in tests
result = await RefineLoop(generator, evaluator, runner=tracker).run("Draft the migration plan")
save_run(run_record(result, workflow="plan/refine", query="Draft the migration plan",
                    started_at=t0, duration_s=elapsed, tracker=tracker))
```

## Tests

```bash
make test-workflows   # python -m pytest tests/workflows -q
```

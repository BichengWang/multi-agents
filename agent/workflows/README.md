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

## Evaluators

`RefineLoop` and `BestOfN` need an evaluator with structured output exposing `score`, `passed`,
and `feedback`. Subclass `Verdict` to add domain-specific fields:

```python
from agents import Agent
from agent.workflows import Verdict

class StoryEvaluation(Verdict):
    originality: float

evaluator = Agent(name="Evaluator", instructions="...", output_type=StoryEvaluation)
```

## Router

`Router` asks a classifier agent (`output_type=RouteDecision`: `route`, `confidence`, `reason`)
to pick one `Route(name, description, handler)`; the handler is any `async (query) -> output`,
usually another workflow's `run`. Unknown routes or confidence below `min_confidence` go to
`fallback` (or raise if none is set), and `run(query, force="name")` skips classification. When
the handler returns a `WorkflowResult`, its steps are appended after the `classify` step.

```python
router = Router(classifier, [Route("story", "creative writing", story_wf.run), ...], fallback="story")
result = await router.run("Write a heist on the moon")
result.meta  # {'route': 'story', 'confidence': 0.93, 'fallback_used': False, ...}
```

## Example

```python
from agent.workflows import RefineLoop

result = await RefineLoop(generator, evaluator, max_iterations=3, threshold=8).run("idea")
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
    return CheckpointDecision(feedback="Give the protagonist a stronger motive.")

result = await RefineLoop(generator, evaluator, checkpoint=review).run("A moon heist")
```

`cli_checkpoint` is a ready-to-use callback: Enter continues, text steers, `/stop` or EOF stops.
Each decision is recorded as a `checkpoint` step, and `meta["checkpoint_stopped"]` distinguishes
a human stop from normal completion. Stopping does not mark a rejected draft as evaluator-accepted.
The shared CLI enables it for the story route with `--story-checkpoint`; the dedicated story CLI
uses `--checkpoint` in refine mode.

## Run artifacts

Wrap the runner in a `RunTracker` to time every agent call. With no base runner it calls the SDK
itself, adds a tracing span per call and records token usage. `run_record()` + `save_run()` then
write the result, verdict, meta, step trace, per-call timings and token totals to `runs/`:

```python
from agent.workflows import RefineLoop, RunTracker, run_record, save_run

tracker = RunTracker()                      # or RunTracker(base=fake_runner) in tests
result = await RefineLoop(generator, evaluator, runner=tracker).run("idea")
save_run(run_record(result, workflow="story/refine", query="idea",
                    started_at=t0, duration_s=elapsed, tracker=tracker))
```

`python -m agent.main` does this for every request (`--runs-dir`, `--no-save`).

## Tests

```bash
make test-workflows   # python -m pytest tests/workflows -q
```

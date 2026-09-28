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

## Tests

```bash
make test-workflows   # python -m pytest tests/workflows -q
```

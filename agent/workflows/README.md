# Workflow Patterns

Reusable orchestration patterns for multi-agent systems. Each pattern takes plain agents plus an
injectable `runner` (`async (agent, input_text) -> final_output`). The default runner uses the
OpenAI Agents SDK (`Runner.run`); tests pass a fake runner so everything runs offline.

| Pattern | Shape | Use when |
|---|---|---|
| `SequentialWorkflow` | A → B → C | Each stage transforms the previous output (plan → write → review). |
| `RefineLoop` | generate ⇄ evaluate | Quality matters and the evaluator's feedback can drive revisions. |
| `BestOfN` | N × (generate → evaluate) in parallel, keep max | You want diversity; revisions are less useful than fresh attempts. |

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

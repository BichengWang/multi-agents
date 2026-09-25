# Workflow protocol

## Runner

```python
AgentRunner = Callable[[Any, str], Awaitable[Any]]
```

`default_runner` runs `agents.Runner.run(agent, input_text)` and returns `final_output`.
Tests use `tests/workflows/fakes.py::fake_runner`, which calls `FakeAgent.respond(input_text)`.

## Verdict

```python
class Verdict(BaseModel):
    score: float     # 1-10 overall
    passed: bool     # ready to ship as-is
    feedback: str    # actionable revision notes
```

`RefineLoop` accepts a draft when `score >= threshold` (if a threshold is set) else when `passed`.
It raises `TypeError` if the evaluator output lacks these fields.

## WorkflowResult

| field | meaning |
|---|---|
| `final_output` | chosen output (last stage for sequential; best draft for refine / best-of-n) |
| `verdict` | the evaluator verdict for `final_output` (refine / best-of-n) |
| `meta` | refine: `iterations`, `accepted`, `scores`; best-of-n: `chosen` (1-based), `scores` |
| `steps` | `StepRecord(name, agent, input, output, iteration)` for every agent call |

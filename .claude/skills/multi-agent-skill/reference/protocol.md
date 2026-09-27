# Agent protocol

Contracts that keep workflows in `agent/` composable.

## Agents
- One module-level `Agent` per file in `my_agents/`, named `<Domain><Role>Agent`.
- Set the model with `agent.config.model_for("<role>")`. The default is overridden per role via `AGENT_MODEL_<ROLE>`.
- Set `output_type` (a Pydantic model) whenever code reads fields from the result. Plain text is only for outputs that are just passed on to the next agent or shown to a person.

## Evaluation
Evaluators return `agent.core.Evaluation`:

| field | type | meaning |
|---|---|---|
| `score` | int 1-10 | overall quality |
| `passed` | bool | shippable as is |
| `strengths` | list[str] | what works |
| `issues` | list[str] | concrete problems, most important first |
| `suggestions` | str | actionable revision instructions |

`refine_loop` stops when `passed` is true and `score >= threshold`. Otherwise it feeds `issues` and `suggestions` back to the generator.

## Handoffs
`handoffs=` takes `Agent` or `handoff()` objects. If agents reference each other, create them first and assign `.handoffs` afterwards.

## Running
New managers call `agent.core.run_agent` (timeout and retries) or a pattern built on it, never `Runner.run` directly, except for streaming (`Runner.run_streamed`).

## Testing
Tests stay offline: monkeypatch `agent.core.runner.run_agent` with a fake that returns `SimpleNamespace(final_output=..., final_output_as=lambda _: ...)`.

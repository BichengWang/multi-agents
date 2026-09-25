---
name: multi-agent-skill
description: create multi agent framework, include the method, script, reference. Use when adding a new multi-agent workflow under agent/ or extending the shared patterns in agent/workflows.
---

# multi-agent-skill

help create a multi-agent framework.

## core logic view

```
query ──► [pattern] ──► WorkflowResult(final_output, verdict, meta, steps)
             │
             ├─ SequentialWorkflow : A → B → C
             ├─ RefineLoop         : generate ⇄ evaluate (feedback) until accepted
             └─ BestOfN            : N × (generate → evaluate) in parallel, keep best
```

Agents are plain OpenAI Agents SDK `Agent`s. Patterns live in `agent/workflows/` and never call
the SDK directly; they call an injected `runner(agent, input_text) -> final_output`.

## input / output

- Input: a text query, plus the agents for each role.
- Evaluator agents used by `RefineLoop` / `BestOfN` must set `output_type` to a subclass of
  `agent.workflows.Verdict` (`score`, `passed`, `feedback`, + domain fields).
- Output: `WorkflowResult` — see `reference/protocol.md`.

## detail method

1. Create `agent/<name>/my_agents/*.py`, one `Agent` per role (keep prompts in module constants).
2. Give the evaluator a `Verdict` subclass as `output_type`.
3. Write `agent/<name>/manager.py` that builds one of the patterns and accepts `runner=` so tests
   can inject a fake.
4. Write `agent/<name>/main.py` (argparse; run with `python -m agent.<name>.main`).
5. Add offline tests under `tests/workflows/` using `tests/workflows/fakes.py`
   (`FakeAgent`, `fake_runner`, `scripted_evaluator`); run `make test-workflows`.
6. Add the module to `tests/workflows/test_imports.py`.

Reference implementation: `agent/story_agent_simple/`.

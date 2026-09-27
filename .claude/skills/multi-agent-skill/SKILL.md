---
name: multi-agent-skill
description: Scaffold or extend a multi-agent workflow in this repo on top of agent/core (pipeline, fan_out, refine_loop, handoffs) with the OpenAI Agents SDK, plus its tests and eval. Use when asked to create a new agent workflow, add an agent to one, or pick an orchestration pattern.
---

# multi-agent-skill

Build multi-agent workflows the same way as the existing ones in `agent/`.

## Core logic view

```
query ─▶ manager.py ──uses──▶ agent.core pattern ──runs──▶ my_agents/*.py (Agent)
                                   │                            │
                                   └── run_agent (retries) ◀────┘
evaluator agents return agent.core.Evaluation ─▶ refine_loop / eval harness branch on it
```

Pick the pattern from the task shape:

| Task shape | Pattern |
|---|---|
| Fixed stages, each builds on the last | `pipeline` |
| Same step over many independent inputs | `fan_out` |
| Output quality can be judged and improved | `refine_loop` (evaluator uses `output_type=Evaluation`) |
| A router picks one specialist | `Agent(handoffs=[agent_a, agent_b])`: pass Agent objects, never names |
| One agent calls specialists mid-task | `specialist.as_tool(...)` |

## Input / output

- **Input**: workflow name, goal, and the agents/stages it needs (ask when unclear).
- **Output**:
  - `agent/<name>/my_agents/<role>_agent.py`: one `Agent` per file with `model=model_for("<role>")`, plus an `output_type` wherever a later step reads fields.
  - `agent/<name>/manager.py`: a `<Name>Manager` class with `async run(query)` that composes the agents with an `agent.core` pattern.
  - `agent/<name>/main.py`: an `input()` prompt, then `asyncio.run`. Runs as `python -m agent.<name>.main`.
  - `agent/<name>/README.md`: the agent chain, usage and an example query.
  - Tests: add the module to `AGENT_MODULES` in `tests/test_agent_imports.py`. For new orchestration logic, add offline tests that monkeypatch `agent.core.runner.run_agent` (see `tests/test_core_patterns.py`).
  - Eval (optional): register a workflow in `agent/agents_eval/run.py` and add `agent/agents_eval/datasets/<name>.jsonl`.

## Detail method

1. Read `agent/core/patterns.py` and the closest existing workflow (`story_agent_simple` for loops, `store_agent` for pipelines, `financial_research_agent` for tools and parallelism).
2. Write the agents. Keep prompts role-specific and put the output contract in the prompt when using `output_type`.
3. Write the manager with a pattern. Don't call `Runner.run` directly; use `agent.core.run_agent` so retries and timeouts apply.
4. Run `make test` (ruff + offline pytest). Nothing may call a live API at import or collection time.
5. When an API key is available, run the workflow once end to end and, if it's registered, run the eval with a `--baseline`.

See `reference/protocol.md` for the contracts between agents.

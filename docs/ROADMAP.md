# Multi-Agent Roadmap

Iterative plan: each item is one PR, built on the shared patterns in `agent/workflows`.

## Done

- **Workflow patterns + iterative story agent** — `SequentialWorkflow`, `RefineLoop`
  (evaluator-optimizer), `BestOfN` (parallel candidates); `story_agent_simple` gets structured
  `StoryEvaluation` output and `--mode single|refine|best-of-n`; offline tests + CI; fixed broken
  imports in `financial_research_agent`.

- **Eval harness** — `agent/agents_eval/harness.py` + `run.py` score any `async (query) -> str`
  workflow over a JSONL dataset with length/keyword checks and a `Verdict`-typed LLM judge; one
  JSON report per run, `--baseline` prints deltas. Story modes (single / refine / best-of-n) are
  registered, so their score trade-offs are measurable.

- **Store agent on `SequentialWorkflow`** — `agent/story_agent` renamed to `agent/store_agent`
  (it builds store concepts) and its hand-written 4-step chain replaced with `SequentialWorkflow`
  plus an injectable runner and offline test. Next step for it: a structured evaluator so the
  evaluate stage can use `RefineLoop`.

## Next

1. **Router / triage pattern** — a classifier agent picks which specialist workflow handles a
   query (story vs. store concept vs. financial research); one entrypoint `python -m agent.main`.
2. **Run artifacts** — persist each `WorkflowResult` (steps, scores, timings, token usage) as JSON
   under `runs/`, and add SDK tracing spans per step.
3. **Human-in-the-loop checkpoint** — optional approval step between iterations (CLI prompt), for
   steering the refine loop.
4. **Debate / panel pattern** — multiple evaluator personas score in parallel, aggregated verdict
   (mean / min / majority) to reduce single-judge bias.

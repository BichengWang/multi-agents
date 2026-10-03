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

- **Router / triage** — `Router` pattern (classifier agent with structured `RouteDecision`,
  confidence threshold, fallback, forced route) and `agent/triage`, which dispatches to the story,
  store, and financial workflows; single entrypoint `python -m agent.main`.

- **Run artifacts** — `RunTracker` wraps any runner to time each agent call; in SDK mode it adds a
  tracing span per call and records token usage. `run_record()` / `save_run()` persist output,
  verdict, meta, step trace, timings and token totals as JSON under `runs/`; `python -m agent.main`
  saves every run (`--runs-dir`, `--no-save`) inside one SDK trace.

- **Human-in-the-loop checkpoint** — optional async `RefineLoop` callback can continue, steer the
  next revision, or stop with the best draft so far. Story CLI `--checkpoint` and shared CLI
  `--story-checkpoint` prompt between iterations; decisions are preserved in run artifacts.

## Next

1. **Debate / panel pattern** — multiple evaluator personas score in parallel, aggregated verdict
   (mean / min / majority) to reduce single-judge bias.

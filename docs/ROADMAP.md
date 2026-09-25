# Multi-Agent Roadmap

Iterative plan: each item is one PR, built on the shared patterns in `agent/workflows`.

## Done

- **Workflow patterns + iterative story agent** — `SequentialWorkflow`, `RefineLoop`
  (evaluator-optimizer), `BestOfN` (parallel candidates); `story_agent_simple` gets structured
  `StoryEvaluation` output and `--mode single|refine|best-of-n`; offline tests + CI; fixed broken
  imports in `financial_research_agent`.

## Next

1. **Router / triage pattern** — a classifier agent picks which specialist workflow handles a
   query (story vs. store concept vs. financial research); one entrypoint `python -m agent.main`.
2. **Port `story_agent` (store concepts) onto the patterns** — replace the hand-written 4-step
   chain with `SequentialWorkflow`; make the evaluator structured so its stage can use `RefineLoop`;
   remove the unused string `handoffs` in the coordinator.
3. **Run artifacts** — persist each `WorkflowResult` (steps, scores, timings, token usage) as JSON
   under `runs/`, and add SDK tracing spans per step.
4. **Bridge to `agents_eval`** — adapter so any workflow can be registered as an agent in
   `EvaluationFramework`; batch-evaluate modes (single vs. refine vs. best-of-n) over a prompt set
   and report score / cost trade-offs.
5. **Human-in-the-loop checkpoint** — optional approval step between iterations (CLI prompt), for
   steering the refine loop.
6. **Debate / panel pattern** — multiple evaluator personas score in parallel, aggregated verdict
   (mean / min / majority) to reduce single-judge bias.

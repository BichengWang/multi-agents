"""CLI for the async eval harness.

Examples:
    python -m agent.agents_eval.run --workflow story_single --dataset agent/agents_eval/datasets/story.jsonl
    python -m agent.agents_eval.run --workflow story_refine --dataset agent/agents_eval/datasets/story.jsonl \
        --baseline agent/agents_eval/results/story_single_<timestamp>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json

from agent.core import refine_loop, runner

from .harness import KeywordCheck, LengthCheck, LLMJudgeCheck, Workflow, compare, load_cases, run_eval


async def _story_single(query: str) -> str:
    from agent.story_agent_simple.my_agents.generator_agent import generator_agent

    return str((await runner.run_agent(generator_agent, query)).final_output)


async def _story_refine(query: str) -> str:
    from agent.story_agent_simple.my_agents.evaluator_agent import evaluator_agent
    from agent.story_agent_simple.my_agents.generator_agent import generator_agent

    return (await refine_loop(generator_agent, evaluator_agent, query)).output


WORKFLOWS: dict[str, Workflow] = {
    "story_single": _story_single,
    "story_refine": _story_refine,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workflow", choices=sorted(WORKFLOWS), required=True)
    parser.add_argument("--dataset", required=True, help="JSONL file of EvalCase records")
    parser.add_argument("--output-dir", default="agent/agents_eval/results")
    parser.add_argument("--baseline", help="Saved report JSON to compare against")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge (deterministic checks only)")
    args = parser.parse_args()

    checks = [LengthCheck(min_chars=200), KeywordCheck(min_ratio=0.5)]
    if not args.no_judge:
        checks.append(LLMJudgeCheck())

    report = asyncio.run(
        run_eval(args.workflow, WORKFLOWS[args.workflow], load_cases(args.dataset), checks, concurrency=args.concurrency)
    )
    path = report.save(args.output_dir)
    print(json.dumps(report.summary(), indent=2))
    print(f"Saved report to {path}")

    if args.baseline:
        with open(args.baseline) as f:
            print("Delta vs baseline:")
            print(json.dumps(compare(json.load(f), report.to_dict()), indent=2))


if __name__ == "__main__":
    main()

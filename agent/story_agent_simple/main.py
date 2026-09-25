import argparse
import asyncio

from agent.story_agent_simple.manager import SimpleStoryManager


# Entrypoint for the simple story agent workflow.
# Run this as `python -m agent.story_agent_simple.main` and enter a story concept query.
async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and evaluate a story concept.")
    parser.add_argument("query", nargs="?", help="Story concept (prompted for if omitted)")
    parser.add_argument("--mode", choices=["single", "refine", "best-of-n"], default="refine")
    parser.add_argument("--max-iterations", type=int, default=3, help="refine mode only")
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="refine mode: accept once score >= threshold (default: evaluator's `passed`)",
    )
    parser.add_argument("-n", type=int, default=3, help="best-of-n mode: number of candidates")
    args = parser.parse_args()

    query = args.query or input("Enter a story concept or idea to analyze: ")
    mgr = SimpleStoryManager(
        mode=args.mode, max_iterations=args.max_iterations, threshold=args.threshold, n=args.n
    )
    await mgr.run(query)


if __name__ == "__main__":
    asyncio.run(main())

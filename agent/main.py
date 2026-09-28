import argparse
import asyncio

from agent.triage.manager import TriageManager, build_routes


# Single entrypoint for all multi-agent workflows.
# Run this as `python -m agent.main "your request"`; a triage agent picks the workflow.
async def main() -> None:
    route_names = [r.name for r in build_routes()]
    parser = argparse.ArgumentParser(description="Route a request to the right multi-agent workflow.")
    parser.add_argument("query", nargs="?", help="Request (prompted for if omitted)")
    parser.add_argument("--route", choices=route_names, help="Skip triage and use this workflow")
    parser.add_argument(
        "--fallback", choices=route_names,
        help="Workflow to use when triage is unsure (default: stop with an error)",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.5,
        help="Below this triage confidence, use --fallback (default: 0.5)",
    )
    args = parser.parse_args()

    query = args.query or input("What would you like to do? ")
    mgr = TriageManager(fallback=args.fallback, min_confidence=args.min_confidence)
    await mgr.run(query, route=args.route)


if __name__ == "__main__":
    asyncio.run(main())

import argparse
import asyncio
import time
from datetime import datetime, timezone

from agent.triage.manager import TriageManager, build_routes
from agent.workflows import RunTracker, cli_checkpoint, run_record, save_run


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
    parser.add_argument("--runs-dir", default="runs", help="Where to save the run record (default: runs/)")
    parser.add_argument("--no-save", action="store_true", help="Do not save a run record")
    parser.add_argument(
        "--story-checkpoint", action="store_true",
        help="Story route: inspect rejected drafts and steer or stop before revising",
    )
    args = parser.parse_args()

    query = args.query or input("What would you like to do? ")
    tracker = RunTracker()
    mgr = TriageManager(
        runner=tracker, fallback=args.fallback, min_confidence=args.min_confidence,
        story_checkpoint=cli_checkpoint if args.story_checkpoint else None,
    )

    from agents import trace

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    with trace("agent.main"):
        result = await mgr.run(query, route=args.route)

    if not args.no_save:
        record = run_record(
            result,
            workflow=result.meta.get("route", "unknown"),
            query=query,
            started_at=started_at,
            duration_s=time.perf_counter() - start,
            tracker=tracker,
        )
        path = save_run(record, args.runs_dir)
        print(f"\nRun saved to {path} ({record['totals']})")


if __name__ == "__main__":
    asyncio.run(main())

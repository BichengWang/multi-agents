from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .base import WorkflowResult
from .tracking import RunTracker


def to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of workflow outputs (pydantic models, dataclasses, ...) to JSON."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump"):
        return to_jsonable(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


def run_record(
    result: WorkflowResult,
    *,
    workflow: str,
    query: str,
    started_at: str,
    duration_s: float,
    tracker: Optional[RunTracker] = None,
) -> dict[str, Any]:
    """Everything needed to inspect or compare a run later: output, verdict, trace, timings, tokens."""
    return {
        "workflow": workflow,
        "query": query,
        "started_at": started_at,
        "duration_s": round(duration_s, 3),
        "final_output": to_jsonable(result.final_output),
        "verdict": to_jsonable(result.verdict),
        "meta": to_jsonable(result.meta),
        "steps": to_jsonable(result.steps),
        "calls": to_jsonable(tracker.calls) if tracker else [],
        "totals": tracker.totals() if tracker else {},
    }


def save_run(record: dict[str, Any], runs_dir: str | Path = "runs") -> Path:
    """Write a run record to ``<runs_dir>/<UTC timestamp>_<workflow>.json`` and return the path."""
    out = Path(runs_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(record.get("workflow") or "run")).strip("-") or "run"
    path = out / f"{stamp}_{slug}.json"
    path.write_text(json.dumps(record, indent=2))
    return path

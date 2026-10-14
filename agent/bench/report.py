"""Markdown report: resolve rate with Wilson intervals, paired comparison, cost and wall time.

Pure Python statistics (no scipy):

* Wilson score interval for a binomial proportion, 95 % (z = 1.959963984540054);
* exact two-sided McNemar test on the discordant pairs (binomial with p = 0.5), used for the paired
  comparison of a team against a baseline on the same tasks.

Every statistic is computed on the *common* tasks: those with a valid result for every team in the
report. A pair is invalid when its record has an ``error`` (infrastructure, agent crash, timeout,
grading failure); such pairs are excluded and listed explicitly, unless
``count_errors_as_failures`` is set, in which case they count as unresolved.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .run import RESULTS_FILE, load_meta, load_records

Z95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(wins: int, losses: int) -> float:
    """Two-sided exact McNemar p-value for ``wins`` vs ``losses`` discordant pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2 * tail / 2**n)


@dataclass
class TeamStats:
    team: str
    n: int = 0
    resolved: int = 0
    excluded: list[str] = field(default_factory=list)  # task ids with an errored pair
    timed_out: int = 0  # common tasks graded as left after a solve timeout
    costs: list[Optional[float]] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    outcomes: dict[str, bool] = field(default_factory=dict)  # common tasks only

    @property
    def rate(self) -> float:
        return self.resolved / self.n if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.resolved, self.n)

    @property
    def unpriced(self) -> int:
        return sum(1 for c in self.costs if c is None)

    @property
    def priced_costs(self) -> list[float]:
        return [c for c in self.costs if c is not None]


@dataclass
class Paired:
    team: str
    baseline: str
    n: int
    wins: int  # team resolved, baseline did not
    losses: int  # baseline resolved, team did not
    p_value: float
    delta: float


@dataclass
class Summary:
    teams: list[TeamStats]
    baseline: Optional[str]
    paired: list[Paired]
    common_tasks: list[str]
    errored: list[tuple[str, str, str]]  # (task, team, error)
    not_run: list[tuple[str, str]]  # (task, team) pairs in scope without any record
    scoped_tasks: int
    count_errors_as_failures: bool
    meta: dict[str, Any]


def summarize(
    records: Sequence[dict[str, Any]],
    *,
    baseline: Optional[str] = None,
    count_errors_as_failures: bool = False,
    meta: Optional[dict[str, Any]] = None,
    teams: Optional[Sequence[str]] = None,
    task_ids: Optional[Sequence[str]] = None,
) -> Summary:
    """``teams`` / ``task_ids`` restrict the report (default: every team and task in ``records``).

    A task counts only if every reported team has a usable record for it; (task, team) pairs that
    were never run, for instance after a budget halt, are listed as not run, not as errors.
    """
    wanted = set(teams) if teams is not None else None
    in_scope = set(task_ids) if task_ids is not None else None
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        if wanted is not None and rec["team"] not in wanted:
            continue
        if in_scope is not None and rec["task_id"] not in in_scope:
            continue
        latest[(rec["task_id"], rec["team"])] = rec  # later records supersede earlier ones
    if teams is not None:
        team_list = list(dict.fromkeys(teams))
        missing = [t for t in team_list if not any(team == t for _, team in latest)]
        if missing:
            raise ValueError(f"no results for team(s) {missing}")
    else:
        team_list = list(dict.fromkeys(rec["team"] for rec in records))
        team_list = [t for t in team_list if any(team == t for _, team in latest)]
    teams = team_list
    if baseline is not None and baseline not in teams:
        raise ValueError(f"baseline {baseline!r} has no results; teams present: {teams}")
    tasks = sorted(in_scope) if in_scope is not None else sorted({task for task, _ in latest})
    not_run = [(t, team) for t in tasks for team in teams if (t, team) not in latest]

    def valid(rec: Optional[dict[str, Any]]) -> bool:
        if rec is None:
            return False
        return rec.get("error") is None or count_errors_as_failures

    # A task is "common" when every team has a usable record for it.
    common = [
        t for t in tasks if all(valid(latest.get((t, team))) for team in teams)
    ]
    errored = sorted(
        (task, team, rec["error"])
        for (task, team), rec in latest.items()
        if rec.get("error") is not None
    )

    stats: list[TeamStats] = []
    for team in teams:
        st = TeamStats(team)
        st.excluded = sorted(
            t for t in tasks if (rec := latest.get((t, team))) is not None and rec.get("error") is not None
        ) if not count_errors_as_failures else []
        for task in common:
            rec = latest[(task, team)]
            ok = bool(rec.get("resolved")) and rec.get("error") is None
            st.outcomes[task] = ok
            st.n += 1
            st.resolved += ok
            usage = rec.get("usage") or {}
            st.costs.append(usage.get("cost_usd") if usage else None)
            st.timed_out += bool((rec.get("meta") or {}).get("timed_out"))
            if rec.get("duration_s") is not None:
                st.durations.append(float(rec["duration_s"]))
        stats.append(st)

    paired: list[Paired] = []
    if baseline is not None:
        base = next(s for s in stats if s.team == baseline)
        for st in stats:
            if st.team == baseline:
                continue
            wins = sum(1 for t in common if st.outcomes[t] and not base.outcomes[t])
            losses = sum(1 for t in common if base.outcomes[t] and not st.outcomes[t])
            paired.append(
                Paired(
                    team=st.team,
                    baseline=baseline,
                    n=len(common),
                    wins=wins,
                    losses=losses,
                    p_value=mcnemar_exact(wins, losses),
                    delta=(wins - losses) / len(common) if common else 0.0,
                )
            )
    return Summary(
        stats, baseline, paired, common, errored, not_run, len(tasks),
        count_errors_as_failures, meta or {},
    )


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _mean_cost(values: list[Optional[float]]) -> str:
    priced = [v for v in values if v is not None]
    if not priced:
        return "unpriced"
    missing = len(values) - len(priced)
    if not missing:
        return _money(sum(priced) / len(values))
    # Unpriced runs cost something unknown >= 0, so total priced / all runs bounds the mean below.
    return f"≥{_money(sum(priced) / len(values))} ({missing} unpriced)"


def _median_cost(values: list[Optional[float]]) -> str:
    priced = [v for v in values if v is not None]
    if not priced:
        return "unpriced"
    missing = len(values) - len(priced)
    if not missing:
        return _money(statistics.median(priced))
    # No honest bound exists for a median with unknown members; say what was measured.
    return f"{_money(statistics.median(priced))} (priced runs only; {missing} unpriced)"


def _cost_per_resolved(st: TeamStats) -> str:
    priced = st.priced_costs
    if not st.resolved:
        return "n/a (0 resolved)"
    if not priced:
        return "unpriced"
    text = _money(sum(priced) / st.resolved)
    return text if not st.unpriced else f"≥{text} ({st.unpriced} unpriced)"


def _duration(seconds: list[float]) -> str:
    if not seconds:
        return "n/a"
    m = statistics.median(seconds)
    return f"{m / 60:.1f} min" if m >= 90 else f"{m:.0f} s"


def render(summary: Summary) -> str:
    n = len(summary.common_tasks)
    lines = ["# Benchmark report", ""]
    meta = summary.meta
    if meta:
        bits = []
        if meta.get("dataset"):
            bits.append(f"dataset `{meta['dataset']}`")
        if meta.get("since"):
            bits.append(f"tasks created since {meta['since']}")
        if meta.get("seed") is not None:
            bits.append(f"seed {meta['seed']}")
        if bits:
            lines += [", ".join(bits), ""]
    lines.append(
        f"{n} of {summary.scoped_tasks} tasks have a valid result for every team"
        + (" (errored pairs counted as unresolved)" if summary.count_errors_as_failures else "")
        + ". Resolve rate shows the Wilson 95% confidence interval. "
        "Wall time is the median team solve time per task. *Timed out* counts tasks where the "
        "team hit the solve timeout and was graded on what it had produced (cost unknown)."
    )
    lines.append("")
    lines.append(
        "| Team | Resolved | Rate (95% CI) | Mean cost/task | Median cost/task | Cost/resolved | Median wall time | Timed out | Excluded |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for st in summary.teams:
        lo, hi = st.interval
        lines.append(
            "| `{team}` | {res}/{n} | {rate} ({lo}–{hi}) | {mean} | {med} | {cpr} | {wall} | {tmo} | {exc} |".format(
                team=st.team,
                res=st.resolved,
                n=st.n,
                rate=_pct(st.rate),
                lo=_pct(lo),
                hi=_pct(hi),
                mean=_mean_cost(st.costs),
                med=_median_cost(st.costs),
                cpr=_cost_per_resolved(st),
                wall=_duration(st.durations),
                tmo=st.timed_out,
                exc=len(st.excluded),
            )
        )

    if summary.baseline is not None:
        lines += [
            "",
            f"## Paired comparison vs `{summary.baseline}`",
            "",
            "Same tasks for both teams. Wins: the team resolved a task the baseline did not; "
            "losses: the reverse. p is the exact two-sided McNemar test on those discordant "
            "tasks (no multiplicity correction across teams).",
            "",
            "| Team | Tasks | Wins | Losses | Δ resolve rate | p (McNemar exact) |",
            "|---|---|---|---|---|---|",
        ]
        for pr in summary.paired:
            lines.append(
                f"| `{pr.team}` | {pr.n} | {pr.wins} | {pr.losses} | "
                f"{100 * pr.delta:+.1f} pts | {pr.p_value:.4f} |"
            )
        if not summary.paired:
            lines.append("| (baseline only) | | | | | |")

    lines += ["", "## Excluded and errored pairs", ""]
    if summary.count_errors_as_failures:
        lines.append("Errored pairs were counted as unresolved; none excluded.")
    if not summary.errored:
        lines.append("None.")
    else:
        lines.append(
            f"{len(summary.errored)} errored pair(s)"
            + ("" if summary.count_errors_as_failures else "; their tasks are excluded for every team")
            + ":"
        )
        lines.append("")
        for task, team, error in summary.errored:
            lines.append(f"- `{task}` × `{team}`: {error[:200]}")
    lines += ["", "## Not run", ""]
    if not summary.not_run:
        lines.append("None.")
    else:
        lines.append(
            f"{len(summary.not_run)} (task, team) pair(s) have no record (for example after a "
            "budget halt); their tasks are left out of every team's numbers:"
        )
        lines.append("")
        for task, team in summary.not_run[:50]:
            lines.append(f"- `{task}` × `{team}`")
        if len(summary.not_run) > 50:
            lines.append(f"- ... and {len(summary.not_run) - 50} more")
    return "\n".join(lines) + "\n"


def latest_run(meta: dict[str, Any]) -> dict[str, Any]:
    runs = meta.get("runs") or []
    return runs[-1] if runs else {}


def build_report(
    out_dir: str | Path,
    *,
    baseline: Optional[str] = None,
    teams: Optional[Sequence[str]] = None,
    count_errors_as_failures: bool = False,
    canonicalize: Any = None,
) -> str:
    """Report for ``out_dir``. Without ``teams`` it covers the teams (and tasks) of the latest run
    recorded in ``meta.json``, so leftovers of earlier invocations in the same directory neither
    appear nor shrink the common task set; without a ``meta.json`` it covers every record."""
    out = Path(out_dir)
    records = load_records(out / RESULTS_FILE)
    if not records:
        raise ValueError(f"no results in {out / RESULTS_FILE}")
    meta = load_meta(out)
    run = latest_run(meta)
    canon = canonicalize or (lambda spec: spec)
    selected = [canon(t) for t in teams] if teams else (run.get("teams") or None)
    if selected is not None:
        selected = [t for t in selected if any(r["team"] == t for r in records)] or None
    if baseline is not None:
        present = {r["team"] for r in records}
        baseline = baseline if baseline in present else canon(baseline)
    return render(
        summarize(
            records,
            baseline=baseline,
            count_errors_as_failures=count_errors_as_failures,
            meta=run,
            teams=selected,
            task_ids=run.get("task_ids") or None,
        )
    )

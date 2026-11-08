"""Markdown report: resolve rate with Wilson intervals, paired comparison, cost and wall time.

Pure Python statistics (no scipy):

* Wilson score interval for a binomial proportion, 95 % (z = 1.959963984540054);
* exact two-sided McNemar test on the discordant pairs (binomial with p = 0.5), used for the paired
  comparison of a team against a baseline on the same tasks;
* Newcombe's (1998) method 10 interval for that paired difference in resolve rate.

Offline analyses from the stored records (no grading or model calls): extra model cost per extra
resolved task, solve and grading sandbox-hours, unresolved tasks by failure class from the grading
report, and the reviewer's approvals and the judge's ties against resolution.

Every statistic is computed on the *common* tasks: those with a valid result for every team in the
report. A pair is invalid when its record has an ``error`` (infrastructure, agent crash, timeout,
grading failure); such pairs are excluded and listed explicitly, unless
``count_errors_as_failures`` is set, in which case they count as unresolved.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .run import RESULTS_FILE, load_meta, load_records

Z95 = 1.959963984540054

# Modal's list price for the sandbox the runner requests (2 cores, 16 GiB), from the benchmark
# guide's cost estimate; the report states the rate it prices sandbox-hours at.
MODAL_SANDBOX_USD_PER_HOUR = 0.668

# Runs of these roles each work in a fork of the team's sandbox (which stays open for the whole
# solve); every other run works in the team's sandbox itself.
FORKED_ROLES = {"review": frozenset({"reviewer"}), "best": frozenset({"worker", "judge"})}

NO_F2P_RESULT = "no F2P result"
F2P_NOT_ALL_PASS = "F2P ran, not all pass"
P2P_REGRESSION = "P2P regression"
FAILURE_CLASSES = (NO_F2P_RESULT, F2P_NOT_ALL_PASS, P2P_REGRESSION)


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


def newcombe_paired_interval(
    both: int, wins: int, losses: int, neither: int, z: float = Z95
) -> tuple[float, float]:
    """Interval of a paired difference in proportions (team minus baseline): Newcombe (1998), method 10.

    ``wins``: the team succeeded and the baseline did not; ``losses``: the reverse. Each proportion
    gets its Wilson interval, and the two are combined with the correlation of the paired outcomes,
    whose numerator Newcombe continuity-corrects (ad - bc - n/2 when ad > bc, floored at 0).
    """
    n = both + wins + losses + neither
    if n <= 0:
        return (-1.0, 1.0)
    p1, p2 = (both + wins) / n, (both + losses) / n
    l1, u1 = wilson_interval(both + wins, n, z)
    l2, u2 = wilson_interval(both + losses, n, z)
    margins = (both + wins) * (losses + neither) * (both + losses) * (wins + neither)
    numerator: float = both * neither - wins * losses
    if numerator > 0:
        numerator = max(numerator - n / 2, 0.0)
    phi = numerator / math.sqrt(margins) if margins else 0.0
    delta = p1 - p2
    lower = delta - math.sqrt((p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2)
    upper = delta + math.sqrt((u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


def sandbox_seconds(rec: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """(solve, grading) sandbox seconds of one record, ``None`` where it has no timing.

    Solve: the team's own sandbox for the whole solve (``duration_s``) plus every run in a fork of
    it (``FORKED_ROLES``). Grading: ``eval.duration_s``. Sums of timed spans, so a lower bound on
    billed time: sandbox start-up and fork setup are not timed.
    """
    forked = FORKED_ROLES.get(str(rec.get("team", "")).split(":", 1)[0], frozenset())
    solve = rec.get("duration_s")
    if solve is not None:
        solve = float(solve) + sum(
            float(run.get("duration_s") or 0.0)
            for run in rec.get("runs") or []
            if run.get("role") in forked
        )
    grading = (rec.get("eval") or {}).get("duration_s")
    return solve, (float(grading) if grading is not None else None)


def failure_class(rec: dict[str, Any]) -> str:
    """Why an unresolved record failed, from its grading fields alone (no log parsing).

    One of ``FAILURE_CLASSES``: no hidden FAIL_TO_PASS test produced a result; some did but not all
    pass (a test without a result does not pass), with every PASS_TO_PASS test intact; or a
    PASS_TO_PASS test fails. Anything else is named: an errored pair, an empty patch, a grading
    timeout, or tests that pass for a patch rejected outside the hidden test patch's files.
    """
    ev = rec.get("eval") or {}
    if rec.get("error") is not None or not ev:
        return "errored"
    if ev.get("empty_patch"):
        return "empty patch"
    if ev.get("timed_out"):
        return "grading timed out"
    report = ev.get("report") or {}
    if not report:
        return "no grading report"
    f2p = report.get("FAIL_TO_PASS") or {}
    if (report.get("PASS_TO_PASS") or {}).get("failure"):
        return P2P_REGRESSION
    if report.get("resolved"):
        return "patch rejected outside the hidden-test files"
    if not f2p.get("success") and not f2p.get("failure"):
        return NO_F2P_RESULT
    return F2P_NOT_ALL_PASS


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
    records: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)  # common tasks only

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

    def sandbox_hours(self) -> tuple[float, float, int]:
        """Solve and grading sandbox-hours over the common tasks, and how many records lack a time."""
        solve = grading = 0.0
        untimed = 0
        for rec in self.records.values():
            s, g = sandbox_seconds(rec)
            solve += s or 0.0
            grading += g or 0.0
            untimed += s is None or g is None
        return solve / 3600, grading / 3600, untimed

    def failure_classes(self) -> Counter[str]:
        return Counter(failure_class(rec) for t, rec in self.records.items() if not self.outcomes[t])

    def approvals(self) -> Optional[tuple[int, int, int, int]]:
        """Final reviewer verdict vs resolution: (approved and resolved, approved and unresolved,
        rejected and resolved, rejected and unresolved); ``None`` for teams without a reviewer."""
        cells = [0, 0, 0, 0]
        seen = False
        for task, rec in self.records.items():
            meta = rec.get("meta") or {}
            if "accepted" not in meta:
                continue
            seen = True
            cells[2 * (not meta["accepted"]) + (not self.outcomes[task])] += 1
        return (cells[0], cells[1], cells[2], cells[3]) if seen else None

    def judge_ties(self) -> Optional[tuple[int, int]]:
        """(tasks whose top judge score two or more candidates share, tasks judged); ``None`` for
        teams without a judge."""
        ties = judged = 0
        for rec in self.records.values():
            meta = rec.get("meta") or {}
            scores = [s for s in meta.get("scores") or [] if s is not None]
            if "candidates" not in meta or not scores:
                continue
            judged += 1
            ties += scores.count(max(scores)) > 1
        return (ties, judged) if judged else None


@dataclass
class Paired:
    team: str
    baseline: str
    n: int
    wins: int  # team resolved, baseline did not
    losses: int  # baseline resolved, team did not
    p_value: float
    delta: float
    interval: tuple[float, float] = (-1.0, 1.0)  # Newcombe 95% CI of delta
    extra_cost: Optional[float] = None  # team's minus baseline's model cost; None if any is unpriced


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
            st.records[task] = rec
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
            both = sum(1 for t in common if st.outcomes[t] and base.outcomes[t])
            neither = len(common) - both - wins - losses
            priced = not st.unpriced and not base.unpriced
            paired.append(
                Paired(
                    team=st.team,
                    baseline=baseline,
                    n=len(common),
                    wins=wins,
                    losses=losses,
                    p_value=mcnemar_exact(wins, losses),
                    delta=(wins - losses) / len(common) if common else 0.0,
                    interval=newcombe_paired_interval(both, wins, losses, neither),
                    extra_cost=sum(st.priced_costs) - sum(base.priced_costs) if priced else None,
                )
            )
    return Summary(
        stats, baseline, paired, common, errored, not_run, len(tasks),
        count_errors_as_failures, meta or {},
    )


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _money(x: float) -> str:
    # Cheap models cost fractions of a cent per task, and ratios of two-decimal cells drift
    # ($0.17 / $0.061 = 2.8 for a 2.7x ratio), so amounts under $1 get three decimals.
    return f"${x:,.3f}" if 0 < abs(x) < 1 else f"${x:,.2f}"


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


def _share(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "n/a"


def _ratio(x: Optional[float], base: Optional[float]) -> str:
    return f"{x / base:.1f}×" if x is not None and base else "n/a"


def _extra_per_resolved(pr: Paired) -> str:
    net = pr.wins - pr.losses
    if net <= 0:
        return "n/a (no net gain)"
    if pr.extra_cost is None:
        return "unpriced"
    if pr.extra_cost <= 0:
        return "none (no extra cost)"
    return _money(pr.extra_cost / net)


def _render_compute(summary: Summary, rate: Optional[float]) -> list[str]:
    stats = summary.teams
    hours = {st.team: st.sandbox_hours() for st in stats}
    base = next((st for st in stats if st.team == summary.baseline), None)

    def per_task(st: TeamStats) -> Optional[float]:
        """Mean model plus solve-sandbox cost per task; None when a model cost is unknown."""
        if rate is None or st.unpriced or not st.n:
            return None
        return (sum(st.priced_costs) + hours[st.team][0] * rate) / st.n

    lines = [
        "",
        "## Compute",
        "",
        "Sandbox-hours sum timed spans, so they bound billed time from below (sandbox start-up and "
        "fork setup are not timed): the team's own sandbox for the whole solve, plus every run in a "
        "fork of it (`review`: the reviewer; `best`: each worker and the judge; other team kinds: "
        "none counted), plus the grading sandbox.",
    ]
    if rate is None:
        lines[-1] += " No rate was given, so sandbox time is not priced."
    elif rate == MODAL_SANDBOX_USD_PER_HOUR:
        lines[-1] += (
            f" Priced at ${rate:g} per sandbox-hour, Modal's list price for the runner's sandbox "
            "(2 cores, 16 GiB; see the benchmark guide's cost estimate); "
            "`--sandbox-usd-per-hour` sets another rate."
        )
    else:
        lines[-1] += f" Priced at ${rate:g} per sandbox-hour (`--sandbox-usd-per-hour`)."
    header = ["Team", "Solve sandbox-hours"]
    if base is not None:
        header.append("vs baseline")
    header.append("Grading sandbox-hours")
    if rate is not None:
        header += ["Solve sandbox cost/task", "Model + solve sandbox cost/task"]
        if base is not None:
            header.append("vs baseline")
    lines += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for st in stats:
        solve, grading, untimed = hours[st.team]
        cells = [f"`{st.team}`", f"{solve:.1f} h" + (f" ({untimed} untimed)" if untimed else "")]
        if base is not None:
            cells.append(_ratio(solve, hours[base.team][0]))
        cells.append(f"{grading:.1f} h")
        if rate is not None:
            total = per_task(st)
            cells.append(_money(solve * rate / st.n) if st.n else "n/a")
            cells.append(_money(total) if total is not None else "unpriced")
            if base is not None:
                cells.append(_ratio(total, per_task(base)))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_failures(summary: Summary) -> list[str]:
    lines = [
        "",
        "## Unresolved tasks by failure class",
        "",
        "From each unresolved record's grading report, without reading logs. *No F2P result*: none "
        "of the hidden FAIL_TO_PASS tests produced a result (for example, the hidden tests do not "
        "compile against the patch). *F2P ran, not all pass*: some did, and at least one failed or "
        "produced none, while no PASS_TO_PASS test fails. *P2P regression*: a PASS_TO_PASS test "
        "fails. *Other*: an empty patch, a grading timeout, a patch rejected outside the hidden "
        "test patch's files, or an errored pair counted as unresolved.",
        "",
        "| Team | Unresolved | No F2P result | F2P ran, not all pass | P2P regression | Other |",
        "|---|---|---|---|---|---|",
    ]
    for st in summary.teams:
        classes = st.failure_classes()
        other = {k: v for k, v in classes.items() if k not in FAILURE_CLASSES}
        other_cell = str(sum(other.values()))
        if other:
            other_cell += " (" + ", ".join(f"{v} {k}" for k, v in sorted(other.items())) + ")"
        lines.append(
            f"| `{st.team}` | {sum(classes.values())} | "
            + " | ".join(str(classes[k]) for k in FAILURE_CLASSES)
            + f" | {other_cell} |"
        )
    return lines


def _render_evaluators(summary: Summary) -> list[str]:
    reviewed = [(st, cells) for st in summary.teams if (cells := st.approvals()) is not None]
    judged = [(st, ties) for st in summary.teams if (ties := st.judge_ties()) is not None]
    if not reviewed and not judged:
        return []
    lines = ["", "## Reviewer and judge"]
    if reviewed:
        lines += [
            "",
            "The reviewer's verdict on each final patch (`meta.accepted`) against resolution. "
            "Precision: resolved among approved; specificity: rejected among unresolved.",
            "",
            "| Team | Tasks | Approved, resolved | Approved, unresolved | Rejected, resolved | "
            "Rejected, unresolved | Precision | Specificity |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for st, (tp, fp, fn, tn) in reviewed:
            lines.append(
                f"| `{st.team}` | {tp + fp + fn + tn} | {tp} | {fp} | {fn} | {tn} | "
                f"{_share(tp, tp + fp)} | {_share(tn, tn + fp)} |"
            )
    if judged:
        lines += [
            "",
            "Tasks where two or more candidates share the judge's top score (ties go to the "
            "earliest candidate).",
            "",
            "| Team | Tasks judged | Top score tied |",
            "|---|---|---|",
        ]
        for st, (ties, n) in judged:
            lines.append(f"| `{st.team}` | {n} | {_share(ties, n)} |")
    return lines


def render(summary: Summary, *, sandbox_usd_per_hour: Optional[float] = None) -> str:
    """``sandbox_usd_per_hour`` prices sandbox-hours; without it they are reported unpriced."""
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
        if meta.get("backend"):
            # Wall time depends on it (a local daemon may emulate amd64).
            bits.append(f"{meta['backend']} sandboxes")
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
            "tasks (no multiplicity correction across teams). The 95% CI of Δ is Newcombe's "
            "(1998) method 10 for paired proportions. Extra cost per extra resolved task: the "
            "team's total model cost minus the baseline's, divided by wins minus losses.",
            "",
            "| Team | Tasks | Wins | Losses | Δ resolve rate | 95% CI of Δ | p (McNemar exact) "
            "| Extra model cost per extra resolved |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for pr in summary.paired:
            lo, hi = pr.interval
            lines.append(
                f"| `{pr.team}` | {pr.n} | {pr.wins} | {pr.losses} | "
                f"{100 * pr.delta:+.1f} pts | {100 * lo:+.1f} to {100 * hi:+.1f} pts | "
                f"{pr.p_value:.4f} | {_extra_per_resolved(pr)} |"
            )
        if not summary.paired:
            lines.append("| (baseline only) | | | | | | | |")

    if n:
        lines += _render_compute(summary, sandbox_usd_per_hour)
        lines += _render_failures(summary)
        lines += _render_evaluators(summary)

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
    sandbox_usd_per_hour: Optional[float] = None,
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
        ),
        sandbox_usd_per_hour=sandbox_usd_per_hour,
    )

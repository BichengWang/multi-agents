import json
import math

import pytest

from agent.bench.cli import main
from agent.bench.report import (
    F2P_NOT_ALL_PASS,
    NO_F2P_RESULT,
    P2P_REGRESSION,
    build_report,
    failure_class,
    mcnemar_exact,
    newcombe_paired_interval,
    render,
    sandbox_seconds,
    summarize,
    wilson_interval,
)


def test_wilson_matches_hand_computed_values():
    # n=10: z^2 = 3.841459; 0 successes -> upper = z^2/(n+z^2) = 0.277533.
    lo, hi = wilson_interval(0, 10)
    assert lo == pytest.approx(0.0) and hi == pytest.approx(3.841459 / 13.841459, abs=1e-6)
    # 10/10 mirrors it.
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(10 / 13.841459, abs=1e-6)
    # 8/10 against the published Wilson table value (0.4902, 0.9433).
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4)
    assert hi == pytest.approx(0.9433, abs=1e-4)
    # Both endpoints solve the defining equation (p_hat - p)^2 = z^2 p (1 - p) / n.
    for n, k in ((10, 8), (100, 50), (37, 5)):
        for bound in wilson_interval(k, n):
            assert (k / n - bound) ** 2 == pytest.approx(
                1.959963984540054**2 * bound * (1 - bound) / n, rel=1e-9
            )
    # 50/100 is symmetric around 0.5.
    lo, hi = wilson_interval(50, 100)
    assert (lo + hi) / 2 == pytest.approx(0.5)
    assert lo == pytest.approx(0.4038, abs=1e-4)
    # No data: the vacuous interval.
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_mcnemar_exact_matches_binomial_tail():
    assert mcnemar_exact(0, 0) == 1.0
    # 8 vs 2 of 10 discordant: 2 * (1 + 10 + 45) / 2^10.
    assert mcnemar_exact(8, 2) == pytest.approx(112 / 1024)
    assert mcnemar_exact(2, 8) == mcnemar_exact(8, 2)
    # 9 vs 0 of 9: 2 / 2^9.
    assert mcnemar_exact(9, 0) == pytest.approx(2 / 512)
    # Perfectly balanced is capped at 1.
    assert mcnemar_exact(5, 5) == 1.0
    # 3 vs 1 of 4: 2 * (1 + 4) / 16.
    assert mcnemar_exact(3, 1) == pytest.approx(0.625)
    # Large, balanced-ish case stays a probability.
    assert 0 < mcnemar_exact(40, 25) < 1 and math.isfinite(mcnemar_exact(400, 300))


def rec(task, team, resolved, cost, seconds, error=None):
    return {
        "task_id": task,
        "team": team,
        "resolved": None if error else resolved,
        "usage": None if error else {"cost_usd": cost},
        "duration_s": None if error else seconds,
        "error": error,
    }


def records():
    out = []
    a = {"t1": 1, "t2": 1, "t3": 0, "t4": 0, "t5": 1, "t6": 0}
    b = {"t1": 1, "t2": 1, "t3": 1, "t4": 1, "t5": 0, "t6": 1}
    c = {"t1": 1, "t2": 0, "t3": 0, "t4": 0, "t5": 1}
    b_cost = {"t1": 0.3, "t2": 0.2, "t3": 0.4, "t4": 0.6, "t5": 0.5, "t6": 0.9}
    for t, r in a.items():
        out.append(rec(t, "solo:worker=claude", bool(r), 0.10, 60))
    for t, r in b.items():
        out.append(rec(t, "review:coder=claude,reviewer=claude", bool(r), b_cost[t], 120))
    for t, r in c.items():
        out.append(rec(t, "best:workers=claude+claude", bool(r), None if t == "t3" else 0.2, 90))
    out.append(rec("t6", "best:workers=claude+claude", None, None, None, error="solve: boom"))
    return out


def test_summary_statistics_on_common_tasks_with_paired_comparison():
    s = summarize(records(), baseline="solo:worker=claude")
    assert s.common_tasks == ["t1", "t2", "t3", "t4", "t5"]  # t6 errored for one team
    solo, review, best = s.teams
    assert (solo.resolved, solo.n) == (3, 5)
    assert (review.resolved, review.n) == (4, 5)
    assert (best.resolved, best.n) == (2, 5)
    assert best.excluded == ["t6"] and solo.excluded == [] and review.excluded == []
    paired = {p.team: p for p in s.paired}
    assert (paired[review.team].wins, paired[review.team].losses) == (2, 1)
    assert paired[review.team].p_value == pytest.approx(1.0)  # 2*(1+3)/8
    assert paired[review.team].delta == pytest.approx(0.2)
    assert (paired[best.team].wins, paired[best.team].losses) == (0, 1)
    assert paired[best.team].p_value == pytest.approx(1.0)
    assert [(t, team) for t, team, _ in s.errored] == [("t6", "best:workers=claude+claude")]


def table(text, heading_prefix="| Team | Resolved"):
    """First Markdown table after ``heading_prefix`` as {team: {column: cell}}."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_prefix))
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    rows = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = dict(zip(header, cells))
    return rows


def test_markdown_table_reports_rates_costs_wall_time_and_exclusions():
    text = render(
        summarize(records(), baseline="solo:worker=claude", meta={"dataset": "multilang", "seed": 3})
    )
    assert "multilang" in text and "seed 3" in text
    rows = table(text)
    solo = rows["solo:worker=claude"]
    assert solo["Resolved"] == "3/5" and solo["Rate (95% CI)"].startswith("60.0%")
    assert solo["Mean cost/task"] == "$0.100" and solo["Median cost/task"] == "$0.100"
    assert solo["Cost/resolved"] == "$0.167"  # 0.50 total / 3 resolved
    assert solo["Median wall time"] == "60 s" and solo["Excluded"] == "0"
    review = rows["review:coder=claude,reviewer=claude"]
    assert review["Resolved"] == "4/5" and review["Mean cost/task"] == "$0.400"
    assert review["Cost/resolved"] == "$0.500" and review["Median wall time"] == "2.0 min"
    best = rows["best:workers=claude+claude"]
    assert best["Excluded"] == "1" and "unpriced" in best["Mean cost/task"]
    paired = table(text, "| Team | Tasks")
    assert paired["review:coder=claude,reviewer=claude"]["Wins"] == "2"
    assert paired["review:coder=claude,reviewer=claude"]["Losses"] == "1"
    assert "t6" in text and "solve: boom" in text  # the errored pair is named


def test_unpriced_costs_are_not_presented_as_lower_bounds_when_they_are_not():
    # Costs 1.0, 1.0, 1.0 and one unknown: the priced-only mean (1.0) overstates a lower bound;
    # the true mean is >= 3/4 and the median has no bound at all.
    recs = [rec(f"t{i}", "a", True, 1.0, 10) for i in range(3)]
    recs.append(rec("t3", "a", True, None, 10))
    st = summarize(recs).teams[0]
    assert st.unpriced == 1
    text = render(summarize(recs))
    row = table(text)["a"]
    assert row["Mean cost/task"].startswith("≥$0.75")
    assert "≥" not in row["Median cost/task"] and "unpriced" in row["Median cost/task"]
    assert row["Cost/resolved"].startswith("≥$0.75")  # 3.0 priced / 4 resolved
    clean = table(render(summarize(recs[:3])))["a"]
    assert clean["Mean cost/task"] == "$1.00" and clean["Median cost/task"] == "$1.00"


def test_report_covers_only_the_latest_run_and_lists_pairs_that_never_ran():
    old = [rec(t, "solo:worker=codex", True, 0.1, 5) for t in ("t1", "t2", "t9")]
    new = [
        rec("t1", "solo:worker=claude", True, 0.2, 5),
        rec("t2", "solo:worker=claude", False, 0.2, 5),
        rec("t1", "best:workers=claude+claude", True, 0.5, 9),
        # best was halted before t2: no record at all
    ]
    allrecs = old + new
    s = summarize(
        allrecs,
        baseline="solo:worker=claude",
        teams=["solo:worker=claude", "best:workers=claude+claude"],
        task_ids=["t1", "t2"],
    )
    assert [t.team for t in s.teams] == ["solo:worker=claude", "best:workers=claude+claude"]
    assert s.common_tasks == ["t1"]  # t2 lacks a best record; old-run leftovers are out of scope
    assert s.not_run == [("t2", "best:workers=claude+claude")] and s.errored == []
    assert "t2" in render(s)
    with pytest.raises(ValueError, match="no results for team"):
        summarize(allrecs, teams=["solo:worker=gemini"])


def test_timed_out_pairs_are_graded_and_counted_not_excluded():
    recs = [rec("t1", "a", True, 1.0, 5), rec("t2", "a", False, 1.0, 5)]
    timed = rec("t1", "b", False, None, 3600)
    timed["meta"] = {"timed_out": True}
    timed["usage"] = {"cost_usd": None}
    recs += [timed, rec("t2", "b", True, 0.5, 9)]
    s = summarize(recs, baseline="a")
    assert s.common_tasks == ["t1", "t2"] and s.errored == []
    b = next(t for t in s.teams if t.team == "b")
    assert b.timed_out == 1 and b.resolved == 1 and b.unpriced == 1


def test_errors_can_count_as_failures_and_later_records_supersede_earlier_ones():
    s = summarize(records(), baseline="solo:worker=claude", count_errors_as_failures=True)
    assert len(s.common_tasks) == 6
    best = next(t for t in s.teams if t.team.startswith("best"))
    assert (best.resolved, best.n, best.excluded) == (2, 6, [])
    assert "unresolved" in render(s)
    # A retried pair replaces its errored record.
    retried = records() + [rec("t6", "best:workers=claude+claude", True, 0.2, 90)]
    s = summarize(retried)
    assert len(s.common_tasks) == 6 and s.errored == []
    assert next(t for t in s.teams if t.team.startswith("best")).resolved == 3


def test_unknown_baseline_is_an_error():
    with pytest.raises(ValueError):
        summarize(records(), baseline="solo:worker=codex")


def test_report_command_reads_results_dir(tmp_path, capsys):
    (tmp_path / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records()) + "\n{torn line", encoding="utf-8"
    )
    (tmp_path / "meta.json").write_text(
        json.dumps({"runs": [{"dataset": "multilang", "since": "2026-03-01"}]})
    )
    assert main(["report", str(tmp_path), "--baseline", "solo:worker=claude"]) == 0
    out = capsys.readouterr().out
    assert "2026-03-01" in out and "Paired comparison" in out
    assert "solo:worker=claude" in build_report(tmp_path)


def test_newcombe_interval_matches_a_published_example():
    # StatsDirect's worked example of Newcombe's method 10 (Armitage and Berry 1994, p. 138: 50
    # sputum specimens, 20 grew on both media, 12 on the first only, 2 on the second only).
    lo, hi = newcombe_paired_interval(20, 12, 2, 16)
    assert lo == pytest.approx(0.056156, abs=1e-6) and hi == pytest.approx(0.329207, abs=1e-6)
    # Swapping the teams mirrors the interval; every interval holds its point estimate.
    for cells in ((20, 12, 2, 16), (27, 2, 0, 19), (1, 1, 7, 12), (0, 3, 0, 0)):
        both, wins, losses, neither = cells
        lo, hi = newcombe_paired_interval(*cells)
        assert newcombe_paired_interval(both, losses, wins, neither) == pytest.approx((-hi, -lo))
        assert -1 <= lo <= (wins - losses) / sum(cells) <= hi <= 1
    # No discordant pairs and no variance in either proportion: symmetric around 0.
    lo, hi = newcombe_paired_interval(10, 0, 0, 0)
    assert lo == pytest.approx(-hi) and hi > 0
    assert newcombe_paired_interval(0, 0, 0, 0) == (-1.0, 1.0)


def graded(task, team, resolved, cost, seconds, *, runs=(), meta=None, report=None, grade_s=1800.0, **ev):
    """A done record with runs, team meta and a grading report (lists of test names)."""
    out = rec(task, team, resolved, cost, seconds)
    out["runs"] = [{"role": role, "duration_s": d} for role, d in runs]
    out["meta"] = meta or {}
    out["eval"] = {
        "resolved": resolved, "duration_s": grade_s, "empty_patch": False, "timed_out": False,
        "report": report, **ev,
    }
    return out


def grading_report(f2p_ok=(), f2p_bad=(), f2p_missing=(), p2p_bad=(), resolved=False):
    return {
        "resolved": resolved,
        "FAIL_TO_PASS": {"success": list(f2p_ok), "failure": list(f2p_bad), "missing": list(f2p_missing)},
        "PASS_TO_PASS": {"success": ["p"], "failure": list(p2p_bad), "missing": []},
    }


PASSING = grading_report(f2p_ok=["f"], resolved=True)
NO_RESULT = grading_report(f2p_missing=["f"])
F2P_FAILS = grading_report(f2p_ok=["f1"], f2p_missing=["f2"])
REGRESSION = grading_report(f2p_ok=["f"], p2p_bad=["p2"])
SOLO = "solo:worker=claude"
REVIEW = "review:coder=claude,reviewer=claude"
BEST = "best:workers=claude+claude+claude,judge=claude"


def pilot_like():
    """4 tasks x solo, review, best; every team holds its own sandbox for an hour per task."""
    out = []
    for task, ok, report in (
        ("t1", True, PASSING), ("t2", False, NO_RESULT), ("t3", False, F2P_FAILS), ("t4", True, PASSING)
    ):
        out.append(graded(task, SOLO, ok, 0.1, 3600, runs=[("worker", 3500)], report=report))
    for task, ok, accepted, report in (
        ("t1", True, True, PASSING),
        ("t2", True, True, PASSING),
        ("t3", False, True, REGRESSION),
        ("t4", False, False, NO_RESULT),
    ):
        meta = {"accepted": accepted, "scores": [8.0]}
        runs = [("coder", 3000), ("reviewer", 1800)]
        out.append(graded(task, REVIEW, ok, 0.3, 3600, runs=runs, meta=meta, report=report))
    for task, ok, scores in (
        ("t1", True, [8, 8, 7]), ("t2", False, [9, 7, 6]), ("t3", True, [9, 9, 9]), ("t4", True, [5, None, 6])
    ):
        meta = {"chosen": 1, "scores": scores, "candidates": [{"candidate": i} for i in (1, 2, 3)]}
        runs = [("worker", 1800), ("judge", 0)] * 3
        if task == "t2":
            out.append(graded(task, BEST, ok, 0.5, 3600, runs=runs, meta=meta, grade_s=0.0, empty_patch=True))
        else:
            out.append(graded(task, BEST, ok, 0.5, 3600, runs=runs, meta=meta, report=PASSING))
    return out


def test_sandbox_seconds_add_the_forks_of_review_and_best_teams():
    solo = graded("t", SOLO, True, 0.1, 60, runs=[("worker", 55)])
    review = graded(
        "t", REVIEW, True, 0.1, 100, runs=[("coder", 50), ("reviewer", 20), ("coder", 20), ("reviewer", 10)]
    )
    best = graded("t", BEST, True, 0.1, 90, runs=[("worker", 40), ("judge", 10), ("worker", 50), ("judge", 12)])
    plan = graded("t", "plan:planner=claude,workers=claude", True, 0.1, 70, runs=[("planner", 10), ("worker", 30)])
    assert sandbox_seconds(solo) == (60, 1800)  # the worker works in the team's own sandbox
    assert sandbox_seconds(review) == (130, 1800)
    assert sandbox_seconds(best) == (202, 1800)
    assert sandbox_seconds(plan) == (70, 1800)  # forks of other team kinds are not counted
    assert sandbox_seconds(rec("t", SOLO, None, None, None, error="solve: boom")) == (None, None)


def test_failure_class_reads_only_the_grading_report():
    def cls(report=None, **ev):
        return failure_class(graded("t", SOLO, False, 0.1, 5, report=report, **ev))

    assert cls(NO_RESULT) == NO_F2P_RESULT
    assert cls(F2P_FAILS) == F2P_NOT_ALL_PASS
    assert cls(grading_report(f2p_bad=["f"])) == F2P_NOT_ALL_PASS
    assert cls(REGRESSION) == P2P_REGRESSION
    assert cls(grading_report(f2p_missing=["f"], p2p_bad=["p2"])) == P2P_REGRESSION  # checked first
    assert cls(PASSING) == "patch rejected outside the hidden-test files"  # tests pass, verdict fails
    assert cls(empty_patch=True) == "empty patch"
    assert cls(timed_out=True) == "grading timed out"
    assert failure_class(rec("t", SOLO, None, None, None, error="evaluate: boom")) == "errored"


def test_report_adds_effect_bounds_compute_failure_classes_and_evaluators():
    s = summarize(pilot_like(), baseline=SOLO)
    review, best = s.teams[1], s.teams[2]
    assert review.approvals() == (2, 1, 0, 1) and best.approvals() is None
    assert best.judge_ties() == (2, 4) and review.judge_ties() is None  # [5, None, 6] is untied
    text = render(s, sandbox_usd_per_hour=2.0)

    paired = table(text, "| Team | Tasks | Wins")
    lo, hi = newcombe_paired_interval(1, 1, 1, 1)
    assert paired[REVIEW]["95% CI of Δ"] == f"{100 * lo:+.1f} to {100 * hi:+.1f} pts"
    assert paired[REVIEW]["Extra model cost per extra resolved"] == "n/a (no net gain)"
    assert paired[BEST]["Extra model cost per extra resolved"] == "$1.60"  # (2.00 - 0.40) / 1

    compute = table(text, "| Team | Solve sandbox-hours")
    teams = (SOLO, REVIEW, BEST)
    assert [compute[t]["Solve sandbox-hours"] for t in teams] == ["4.0 h", "6.0 h", "10.0 h"]
    assert [compute[t]["Grading sandbox-hours"] for t in teams] == ["2.0 h", "2.0 h", "1.5 h"]
    assert compute[REVIEW]["Solve sandbox cost/task"] == "$3.00"
    assert compute[BEST]["Model + solve sandbox cost/task"] == "$5.50"
    assert "$2 per sandbox-hour" in text

    failures = table(text, "| Team | Unresolved")
    columns = ("Unresolved", "No F2P result", "F2P ran, not all pass", "P2P regression", "Other")
    assert [failures[SOLO][c] for c in columns] == ["2", "1", "1", "0", "0"]
    assert [failures[REVIEW][c] for c in columns] == ["2", "1", "0", "1", "0"]
    assert failures[BEST]["Other"] == "1 (1 empty patch)"

    reviewer = table(text, "| Team | Tasks | Approved")[REVIEW]
    cells = ("Approved, resolved", "Approved, unresolved", "Rejected, resolved", "Rejected, unresolved")
    assert [reviewer[c] for c in cells] == ["2", "1", "0", "1"]
    assert reviewer["Precision"] == "2/3 (67%)" and reviewer["Specificity"] == "1/2 (50%)"
    assert table(text, "| Team | Tasks judged")[BEST]["Top score tied"] == "2/4 (50%)"

    # Without a rate, sandbox time is reported in hours only.
    plain = render(s)
    assert "not priced" in plain and "Solve sandbox cost/task" not in plain
    # Teams without a reviewer or a judge get no evaluator section.
    assert "## Reviewer and judge" not in render(summarize(records()))


def test_report_command_states_the_sandbox_rate(tmp_path, capsys):
    (tmp_path / "results.jsonl").write_text("\n".join(json.dumps(r) for r in pilot_like()) + "\n")
    assert main(["report", str(tmp_path), "--baseline", SOLO]) == 0
    assert "$0.668 per sandbox-hour, Modal's list price" in capsys.readouterr().out
    assert main(["report", str(tmp_path), "--sandbox-usd-per-hour", "1.5"]) == 0
    out = capsys.readouterr().out
    assert "$1.5 per sandbox-hour (`--sandbox-usd-per-hour`)" in out and "vs baseline" not in out
    assert main(["report", str(tmp_path), "--sandbox-usd-per-hour", "-1"]) == 2

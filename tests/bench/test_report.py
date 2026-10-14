import json
import math

import pytest

from agent.bench.cli import main
from agent.bench.report import build_report, mcnemar_exact, render, summarize, wilson_interval


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


def test_markdown_table_cost_and_wall_time_columns():
    text = render(summarize(records(), baseline="solo:worker=claude", meta={"dataset": "multilang", "seed": 3}))
    assert "dataset `multilang`" in text and "seed 3" in text
    assert "5 tasks with a valid result for every team" in text
    lines = {}
    for line in text.splitlines():  # first match per team = the main table, not the paired one
        if line.startswith("| `"):
            lines.setdefault(line.split("|")[1].strip(" `"), line)
    solo = lines["solo:worker=claude"]
    assert "| 3/5 | 60.0% (23.1%–88.2%) |" in solo
    assert "| $0.10 | $0.10 | $0.17 | 60 s | 0 |" in solo  # 0.5 / 3 resolved
    review = lines["review:coder=claude,reviewer=claude"]
    assert "| 4/5 | 80.0% (37.6%–96.4%) |" in review
    assert "| $0.40 | $0.40 | $0.50 | 2.0 min | 0 |" in review  # mean/median of .3 .2 .4 .6 .5; 2.0/4
    best = lines["best:workers=claude+claude"]
    assert "≥$0.20 (1 unpriced)" in best and "≥$0.40 (1 unpriced)" in best and best.endswith("| 1 |")
    assert "## Paired comparison vs `solo:worker=claude`" in text
    assert "| `review:coder=claude,reviewer=claude` | 5 | 2 | 1 | +20.0 pts | 1.0000 |" in text
    assert "## Excluded and errored pairs" in text
    assert "- `t6` × `best:workers=claude+claude`: solve: boom" in text


def test_errors_can_count_as_failures_and_later_records_supersede_earlier_ones():
    s = summarize(records(), baseline="solo:worker=claude", count_errors_as_failures=True)
    assert len(s.common_tasks) == 6
    best = next(t for t in s.teams if t.team.startswith("best"))
    assert (best.resolved, best.n, best.excluded) == (2, 6, [])
    assert "counted as unresolved" in render(s)
    # A retried pair replaces its errored record.
    retried = records() + [rec("t6", "best:workers=claude+claude", True, 0.2, 90)]
    s = summarize(retried)
    assert len(s.common_tasks) == 6 and s.errored == []
    assert next(t for t in s.teams if t.team.startswith("best")).resolved == 3


def test_unknown_baseline_is_an_error():
    with pytest.raises(ValueError, match="no results"):
        summarize(records(), baseline="solo:worker=codex")


def test_report_command_reads_results_dir(tmp_path, capsys):
    (tmp_path / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records()) + "\n{torn line", encoding="utf-8"
    )
    (tmp_path / "meta.json").write_text(json.dumps({"dataset": "multilang", "since": "2026-03-01"}))
    assert main(["report", str(tmp_path), "--baseline", "solo:worker=claude"]) == 0
    out = capsys.readouterr().out
    assert "tasks created since 2026-03-01" in out and "Paired comparison" in out
    assert "solo:worker=claude" in build_report(tmp_path)

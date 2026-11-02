# Benchmark report

dataset `multilang`, tasks created since 2026-07-01, seed 7, modal sandboxes

48 of 48 tasks have a valid result for every team. Resolve rate shows the Wilson 95% confidence interval. Wall time is the median team solve time per task. *Timed out* counts tasks where the team hit the solve timeout and was graded on what it had produced (cost unknown).

| Team | Resolved | Rate (95% CI) | Mean cost/task | Median cost/task | Cost/resolved | Median wall time | Timed out | Excluded |
|---|---|---|---|---|---|---|---|---|
| `solo:worker=claude@claude-haiku-5-5` | 27/48 | 56.2% (42.3%–69.3%) | $0.061 | $0.011 | $0.11 | 77 s | 0 | 0 |
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 29/48 | 60.4% (46.3%–73.0%) | $0.098 | $0.019 | $0.16 | 3.3 min | 0 | 0 |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 28/48 | 58.3% (44.3%–71.2%) | $0.17 | $0.070 | $0.29 | 4.4 min | 0 | 0 |

## Paired comparison vs `solo:worker=claude@claude-haiku-5-5`

Same tasks for both teams. Wins: the team resolved a task the baseline did not; losses: the reverse. p is the exact two-sided McNemar test on those discordant tasks (no multiplicity correction across teams).

| Team | Tasks | Wins | Losses | Δ resolve rate | p (McNemar exact) |
|---|---|---|---|---|---|
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 48 | 2 | 0 | +4.2 pts | 0.5000 |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 48 | 2 | 1 | +2.1 pts | 1.0000 |

## Excluded and errored pairs

None.

## Not run

None.

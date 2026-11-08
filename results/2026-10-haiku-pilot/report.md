# Benchmark report

dataset `multilang`, tasks created since 2026-07-01, seed 7, modal sandboxes

48 of 48 tasks have a valid result for every team. Resolve rate shows the Wilson 95% confidence interval. Wall time is the median team solve time per task. *Timed out* counts tasks where the team hit the solve timeout and was graded on what it had produced (cost unknown).

| Team | Resolved | Rate (95% CI) | Mean cost/task | Median cost/task | Cost/resolved | Median wall time | Timed out | Excluded |
|---|---|---|---|---|---|---|---|---|
| `solo:worker=claude@claude-haiku-5-5` | 27/48 | 56.2% (42.3%–69.3%) | $0.061 | $0.011 | $0.109 | 77 s | 0 | 0 |
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 29/48 | 60.4% (46.3%–73.0%) | $0.098 | $0.019 | $0.163 | 3.3 min | 0 | 0 |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 28/48 | 58.3% (44.3%–71.2%) | $0.167 | $0.070 | $0.285 | 4.4 min | 0 | 0 |

## Paired comparison vs `solo:worker=claude@claude-haiku-5-5`

Same tasks for both teams. Wins: the team resolved a task the baseline did not; losses: the reverse. p is the exact two-sided McNemar test on those discordant tasks (no multiplicity correction across teams). The 95% CI of Δ is Newcombe's (1998) method 10 for paired proportions. Extra cost per extra resolved task: the team's total model cost minus the baseline's, divided by wins minus losses.

| Team | Tasks | Wins | Losses | Δ resolve rate | 95% CI of Δ | p (McNemar exact) | Extra model cost per extra resolved |
|---|---|---|---|---|---|---|---|
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 48 | 2 | 0 | +4.2 pts | -2.7 to +10.9 pts | 0.5000 | $0.889 |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 48 | 2 | 1 | +2.1 pts | -5.9 to +10.0 pts | 1.0000 | $5.05 |

## Compute

Sandbox-hours sum timed spans, so they bound billed time from below (sandbox start-up and fork setup are not timed): the team's own sandbox for the whole solve, plus every run in a fork of it (`review`: the reviewer; `best`: each worker and the judge; other team kinds: none counted), plus the grading sandbox. Priced at $0.668 per sandbox-hour, Modal's list price for the runner's sandbox (2 cores, 16 GiB; see the benchmark guide's cost estimate); `--sandbox-usd-per-hour` sets another rate.

| Team | Solve sandbox-hours | vs baseline | Grading sandbox-hours | Solve sandbox cost/task | Model + solve sandbox cost/task | vs baseline |
|---|---|---|---|---|---|---|
| `solo:worker=claude@claude-haiku-5-5` | 3.8 h | 1.0× | 5.1 h | $0.053 | $0.115 | 1.0× |
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 8.8 h | 2.3× | 4.4 h | $0.122 | $0.220 | 1.9× |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 21.7 h | 5.7× | 4.8 h | $0.302 | $0.469 | 4.1× |

## Unresolved tasks by failure class

From each unresolved record's grading report, without reading logs. *No F2P result*: none of the hidden FAIL_TO_PASS tests produced a result (for example, the hidden tests do not compile against the patch). *F2P ran, not all pass*: some did, and at least one failed or produced none, while no PASS_TO_PASS test fails. *P2P regression*: a PASS_TO_PASS test fails. *Other*: an empty patch, a grading timeout, a patch rejected outside the hidden test patch's files, or an errored pair counted as unresolved.

| Team | Unresolved | No F2P result | F2P ran, not all pass | P2P regression | Other |
|---|---|---|---|---|---|
| `solo:worker=claude@claude-haiku-5-5` | 21 | 7 | 11 | 3 | 0 |
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 19 | 7 | 10 | 2 | 0 |
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 20 | 6 | 12 | 2 | 0 |

## Reviewer and judge

The reviewer's verdict on each final patch (`meta.accepted`) against resolution. Precision: resolved among approved; specificity: rejected among unresolved.

| Team | Tasks | Approved, resolved | Approved, unresolved | Rejected, resolved | Rejected, unresolved | Precision | Specificity |
|---|---|---|---|---|---|---|---|
| `review:coder=claude@claude-haiku-5-5,reviewer=claude@claude-haiku-5-5,rounds=3` | 48 | 28 | 19 | 1 | 0 | 28/47 (60%) | 0/19 (0%) |

Tasks where two or more candidates share the judge's top score (ties go to the earliest candidate).

| Team | Tasks judged | Top score tied |
|---|---|---|
| `best:workers=claude@claude-haiku-5-5+claude@claude-haiku-5-5+claude@claude-haiku-5-5,judge=claude@claude-haiku-5-5` | 48 | 32/48 (67%) |

## Excluded and errored pairs

None.

## Not run

None.

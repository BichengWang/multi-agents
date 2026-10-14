# Fresh-task benchmark

Runs team specs (`solo`, `review`, `best`, see [`docs/ROADMAP.md`](../../docs/ROADMAP.md)) on GitHub
issues that were opened *after* the models' training cutoffs, grades the resulting patches with the
dataset's official hidden-test logic in remote Modal sandboxes, and reports team-vs-solo results:
resolve rate with confidence intervals, paired significance, cost and wall time.

```text
dataset.py   load + filter + seeded sample of tasks      sandbox.py   Modal Sandbox = Workspace
evaluate.py  official grading in a fresh sandbox          run.py       task x team -> JSONL (resumable)
report.py    Markdown report, Wilson / McNemar            cli.py       python -m agent.bench ...
```

## Dataset choice

Checked on 2026-10-13. Criteria: per-task creation dates (for contamination filtering), prebuilt
runnable image per task, official grading code, license, language coverage, freshness.

| Candidate | Fresh? | Images | Grading | License | Languages |
|---|---|---|---|---|---|
| **SWE-bench-Live / MultiLang** (chosen) | newest task created 2026-07-31; 231 tasks from July 2026; HF card last updated 2026-09-20 | `docker_image` column, public Docker Hub (`starryzhang/sweb.eval.x86_64.*`) | per-row `test_cmds` / `print_cmds` / `rebuild_cmds` / `log_parser` + official script | MIT | C, C++, C#, Go, Java, JS, Rust, TS |
| SWE-bench-Live / SWE-bench-Live (Python, `full`) | **stalled**: newest task created 2025-09-02 at the pinned revision, despite the "50 per month" promise | derived name (same rule) | same script, `pytest` parser | MIT | Python |
| SWE-rebench-leaderboard (Nebius) | monthly splits to 2026-03 on the Hub (the `test` split reaches 2026-05); the July 2026 split moved to Harbor Hub | `docker_image` on Docker Hub | SWE-bench harness / Harbor verifier (Docker-daemon based) | CC-BY-4.0 | Python only |
| SWE-rebench-V2 (Nebius) | training corpus, 32,079 tasks; newest `created_at` 2025-10 at its latest revision | `image_name` | parsers in a separate repo | CC-BY-4.0 | 20 languages |

**Decision: SWE-bench-Live MultiLang is primary.** It is the only candidate that is both fresh and
fully self-describing: every row carries the image, the exact commands, the log parser and the
expected `FAIL_TO_PASS` / `PASS_TO_PASS` tests, so the official grading procedure can be replayed
inside a Modal sandbox without a Docker daemon. SWE-rebench has a larger Python pool but its grading
depends on the SWE-bench Docker harness (or Harbor), and its newest evaluation split left the Hub.
The `python` dataset key stays available for pre-2025-09 comparisons only.

Why this matters for the pilot: Anthropic documents a **June 2026** training cutoff for every
current model ([models overview](https://platform.claude.com/docs/en/models/overview)). At the pinned
revision, `--since 2026-07-01` leaves **231 tasks from 57 repositories** (Rust 68, Java 51, C 43,
C++ 34, C# 22, JS 13), enough for a 50-task seeded pilot with room for gold-patch filtering.
Counts come from `python -m agent.bench tasks --since 2026-07-01` against the pinned revision.

Evidence:
[SWE-bench-Live repository](https://github.com/microsoft/SWE-bench-Live) (README news, evaluation protocol),
[MultiLang dataset card](https://huggingface.co/datasets/SWE-bench-Live/MultiLang),
[Python dataset card](https://huggingface.co/datasets/SWE-bench-Live/SWE-bench-Live),
[SWE-rebench-leaderboard card](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard),
[SWE-rebench-V2 card](https://huggingface.co/datasets/nebius/SWE-rebench-V2),
[RepoLaunch](https://github.com/microsoft/RepoLaunch) (builds the images, owns the log parsers).
Newest-task and per-month figures were measured on the pinned parquet files, not copied from cards.

### Dataset facts the code relies on

- Hugging Face `SWE-bench-Live/MultiLang`, revision `3638632e8153a10ca422c1022bed79023084b5c9`;
  one parquet per language split: `data/{c,cpp,cs,go,java,js,rust,ts}-00000-of-00001.parquet`.
- Columns: `instance_id`, `repo`, `base_commit`, `problem_statement`, `patch` (gold), `test_patch`,
  `created_at` (ISO string), `docker_image`, `rebuild_cmds`, `test_cmds`, `print_cmds`, `log_parser`
  (a Python script defining `parser(log) -> {test: pass|fail|skip}`), `FAIL_TO_PASS`, `PASS_TO_PASS`.
- Python dataset (`SWE-bench-Live/SWE-bench-Live`, revision `b51a86422e10cfd403beb4773e5a2947953e36ec`,
  `full` split in two shards) has no `docker_image` / `print_cmds`; the image is
  `starryzhang/sweb.eval.x86_64.<instance_id with "__" -> "_1776_", lowercased>` and the parser is
  the built-in pytest one.
- Pins live in `dataset.py` (`SOURCES`); bump them deliberately to pick up newly added tasks.

## Official grading

`evaluate.py` ports `evaluation/evaluation.py` of microsoft/SWE-bench-Live at commit
`62d02936a0726fbf4597b05bd07b09fb99eddae6` plus `apply_patch` of RepoLaunch at
`099857f37e87e7bf09a2baa1c78809518c485c53` (both MIT):

1. In a fresh sandbox of the instance image, apply `test_patch`, then the candidate patch, with
   `git apply --reject --whitespace=nowarn`. A failed apply does not stop the run (as upstream).
2. In one interactive bash session: `rebuild_cmds`, then `test_cmds`, each joined with ` ; `.
3. The test log is the output of `print_cmds`. For rows without them (Python), upstream runs the
   tests as `bash run_test.sh > testlog.out 2>&1` and prints `testlog.out`; so do we.
4. The log is parsed with the row's `log_parser` script (`pytest` selects the built-in parser).
5. **Resolved** iff no `PASS_TO_PASS` test failed, no `FAIL_TO_PASS` test failed, and every
   `FAIL_TO_PASS` test passed. Status matching is upstream's: `"pass" in status.lower()` etc.

Deviations, all forced by running on Modal instead of a local Docker daemon:

- Upstream drives containers through RepoLaunch's Docker runtime; we run the same commands via
  Modal Sandbox exec. RepoLaunch itself (Python >= 3.12, LangChain stack, Docker SDK) is not
  installed; grading needs none of it. The decision logic is a line-for-line port.
- Parser scripts come from the dataset and are executed in an isolated Python subprocess on the
  host (`python -I`, empty environment, 120 s limit), not in the sandbox, because not every instance
  image has Python and because no API key may be reachable by dataset-supplied code.
- Upstream quirk kept for comparability: a `PASS_TO_PASS` test that never appears in the log does
  not fail the instance. It is recorded as `pass_to_pass_missing` in every result so you can audit
  it.
- A hung test run (150 min, upstream's limit) counts as unresolved with `timed_out: true`. A broken
  parser or sandbox failure is an *errored* pair, excluded from statistics and retried on resume.

Upstream recommends validating instances with the gold patch before benchmarking: tests rot, and
results should use only instances whose reference patch resolves on your machine and day.
`python -m agent.bench gold` does exactly that and `run --gold-file` keeps only valid ones.

## Setup

```bash
uv sync --locked --extra dev --extra bench          # add --extra litellm for sdk@litellm/... teams
uv run --extra bench modal token set --token-id ak-... --token-secret as-...
uv run --extra bench modal secret create multi-agents-bench \
    ANTHROPIC_API_KEY=... OPENAI_API_KEY=... [OPENROUTER_API_KEY=...]
```

- **Modal Secret** `multi-agents-bench` (override with `--secret`) is injected into *agent*
  sandboxes only, never into grading sandboxes. `run` derives which keys your teams need
  (`claude` -> `ANTHROPIC_API_KEY`, `codex` and `sdk@<model>` -> `OPENAI_API_KEY`,
  `sdk@litellm/openrouter/...` -> `OPENROUTER_API_KEY`, ...) and Modal fails fast naming any that
  are missing. Teams run with permissions bypassed inside the sandbox
  (`build_team(spec, permissions="bypass")`; Claude Code only accepts `--dangerously-skip-permissions` as root when `IS_SANDBOX=1`, which the sandbox sets, see [this write-up](https://wmedia.es/en/tips/claude-code-dangerously-skip-permissions-what-it-does); Codex gets [`--dangerously-bypass-approvals-and-sandbox`](https://developers.openai.com/codex/cli/reference)), so
  **an agent can read the keys in its sandbox: use keys with a spend limit.**
- Images are pulled from Docker Hub by Modal. If pulls are rate limited, create a secret with
  `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` and pass `--registry-secret NAME`.
- Agent sandboxes extend the instance image with Node.js 24.21.0 (tarball checksum-verified against
  `SHASUMS256.txt`), `@anthropic-ai/claude-code@2.1.287` and `@openai/codex@0.162.1` (pins in
  `sandbox.py`). The layer is cached by Modal per instance image; the first run pays the build.
  Instance images must be glibc-based (Debian/Ubuntu-style; the Node tarball is glibc).
- Each sandbox gets 2 Modal cores (= the dataset's recommended 4 CPUs) and 16 GB; a few large C++
  repositories need more (upstream mentions up to 50 GB).

## Commands

```bash
# 1. No credentials needed: the seeded sample of fresh tasks (downloads the pinned parquet files).
python -m agent.bench tasks --since 2026-07-01 --limit 50 --seed 7

# 2. Keep only tasks whose gold patch still resolves (Modal, no model cost).
python -m agent.bench gold --since 2026-07-01 --out runs/gold --concurrency 8

# 3. Run teams (resumable; re-run the same command to continue).
python -m agent.bench run --since 2026-07-01 --limit 50 --seed 7 \
    --gold-file runs/gold/gold.jsonl \
    --team solo:worker=claude@claude-sonnet-5-5 \
    --team review:coder=claude@claude-sonnet-5-5,reviewer=claude@claude-sonnet-5-5 \
    --team best:workers=claude@claude-sonnet-5-5+claude@claude-sonnet-5-5+claude@claude-sonnet-5-5 \
    --max-cost-usd 400 --concurrency 6 --out runs/pilot

# 4. Report, comparing every team with the solo baseline on the same tasks.
python -m agent.bench report runs/pilot --baseline solo:worker=claude@claude-sonnet-5-5
```

`run` writes to `--out`: `results.jsonl` (one record per task x team: patch, resolved, usage and
cost, duration, runs summary, grading details, error), `meta.json`, and
`artifacts/<team>/<task>/{patch.diff,runs.json,eval.log}` with full harness logs (the raw
trajectories). Filters shared by `tasks`, `gold` and `run`: `--dataset`, `--since` (inclusive),
`--until` (exclusive), `--repo`, `--language`, `--instance-id`, `--limit`, `--seed`.

- **Resume**: finished pairs are skipped; errored pairs are retried (`--no-retry-errors` to keep
  them). An error in the *grading* stage re-grades the saved patch instead of re-running (and
  re-paying for) the agents.
- **Budget** (`--max-cost-usd X`): no new pair starts once priced spend, including earlier runs in
  the same `--out`, reaches X; running pairs finish, so spend can exceed X by up to `--concurrency`
  pairs. A budgeted run refuses to start if a team contains a harness whose cost cannot be priced
  (e.g. `codex` without `@model`) unless `--allow-unpriced`, and stops launching as soon as any
  finished pair reports an unpriced cost.
- **Cancellation**: Ctrl-C cancels in-flight pairs and terminates their sandboxes.
- `--solve-timeout` (default 3600 s) bounds one team solve; a timeout is an errored pair.
- `--keep-history` disables the git-history stripping described below.

## Methodology

- **Contamination.** Pick `--since` after the training cutoff of every model under test, from the
  vendor's own documentation, and report it (it is stored in `meta.json` and printed in the report).
  Anthropic: June 2026 for all current models (link above). OpenAI documents August 2025 for
  `gpt-5.3-codex` ([model page](https://developers.openai.com/api/docs/models/gpt-5.3-codex));
  cutoffs of OpenAI's newer models were not checked, so look them up before mixing providers.
  Creation date is the issue/PR `created_at` in the dataset.
- **One rollout per pair, no feedback.** Agents see only a generic instruction plus the
  `problem_statement`, never `test_patch`, `FAIL_TO_PASS`, hints or any grading field; they never
  see grader output. This follows SWE-bench-Live's evaluation protocol
  ([README](https://github.com/microsoft/SWE-bench-Live#-evaluation)). Teams that loop
  (`review`) use only the agents' own judgment.
- **No leaks through the image.** Instance images are built at the task's base commit but may carry
  later git history. Agent sandboxes strip every ref, remote, reflog entry and unreachable object
  before the agent starts, so the fix commit cannot be read with `git log --all`. The hidden tests
  are only ever written into a separate grading sandbox, which has no secrets and no agent CLIs.
  What cannot be stripped: network access (an agent could in principle fetch the upstream fix; the
  protocol's trajectory-audit remedy applies, and `artifacts/` keeps the trajectories).
- **Patch** = everything changed since the sandbox started (commits, edits, new files,
  `.gitignore` honoured), diffed against a tree snapshot taken at start so build leftovers in the
  image are not attributed to the agent.
- **Statistics** (`report.py`, pure Python): resolve rate with the Wilson 95 % interval; paired
  comparison against the baseline on the *same* tasks with the exact two-sided McNemar test on
  discordant tasks (wins = team solved, baseline did not; losses = the reverse); mean, median and
  per-resolved cost; median wall time. All statistics use the *common* tasks, those with a valid
  result for every team. Errored pairs (infrastructure, agent crash, timeout, grading failure) are
  excluded and listed by name; `--count-errors-as-failures` counts them as unresolved instead. No
  multiplicity correction across teams is applied. With 50 tasks only large effects reach
  significance: 6 wins against 0 losses gives p = 0.031 (5 against 0 gives 0.063), 12 against 3
  gives 0.035; a 4 to 8 point gap is not detectable, so read the intervals.
- **Cost** is `TeamResult.usage` summed over all harness runs. Unpriced runs make a total
  "unpriced" and are shown as lower bounds (`≥$x (k unpriced)`), never silently dropped.

## Cost estimate: 50-task pilot

Anchors, measured by others on a fresh-task benchmark (SWE-rebench leaderboard,
[swe-rebench.com](https://swe-rebench.com/leaderboard), window 2026-05-15 to 2026-07-01,
111 problems; read 2026-10-13): **Claude Code $3.39 per problem** (60.4 % resolved),
**Codex $1.59** (58.0 %), Sonnet 5 [high] $1.43, Opus 5 [high] $3.47, GPT-5.6 Sol [medium] $0.85.
Those are Python tasks at then-current prices; this benchmark's tasks skew to Rust/Java/C/C++ and
need larger builds, so treat the numbers as order of magnitude and replace them with measured ones
(next paragraph).

Modal sandbox rate (from [modal.com/pricing](https://modal.com/pricing), Sandbox rates):
2 cores x $0.00003942 + 16 GiB x $0.00000667 = $0.0001856/s = **$0.668/hour**.

| Config (50 tasks) | LLM cost per task | Sandbox time per pair | LLM total | Sandbox total | Total |
|---|---|---|---|---|---|
| `solo` Claude Code | $3.39 | 20 min agent + 15 min grading = 35 min = $0.39 | $169.50 | $19.50 | **$189** |
| `solo` Codex | $1.59 | 35 min = $0.39 | $79.50 | $19.50 | **$99** |
| `review` Claude coder + Claude reviewer | 1.4x to 3.1x solo = $4.75 to $10.51 | 43 to 75 min = $0.48 to $0.84 | $237 to $525 | $24 to $42 | **$261 to $567** |
| `best` 3 Claude workers + judge | 3.3x solo = $11.19 | 3 x 20 + 10 + 15 = 85 min = $0.95 | $559 | $47 | **$607** |

Multipliers are assumptions, not measurements: a reviewer pass costs about 0.4x of the coder run, a
fix pass 0.3x, the judge 0.3x of one worker. `review` low end = coder + one approving review
(1 + 0.4); high end = the default 3 rounds of review and fix (1 + 3 x (0.4 + 0.3)). Sandbox time
assumes 20 minutes of agent work per worker and 15 minutes of grading. The four configurations
together cost roughly **$1,156 to $1,462**. Image builds (first use of each instance image) are not
estimated. A cheaper pilot swaps the model: solo Sonnet 5 through the SDK agent is $1.43 x 50 = $71.50.

Do not commit to this budget blind: run 5 tasks first
(`--limit 5 --max-cost-usd 40`), read the measured cost per task in `report`, and scale.

## Limitations

- **Not exercised live here.** The Modal sandbox, image build and grading paths are covered by
  offline tests that drive the same code through a fake Modal client running commands in local
  temporary git repositories (diff/apply/fork semantics, history stripping, grading order, parser
  isolation). Nothing was run against real Modal, the real instance images or real model APIs. In
  particular unverified: whether Modal accepts every instance image without Python on `PATH`
  (use `--add-python 3.12` if not), and wall-clock cost per task.
- Teams come from the separate team runner (`agent.teams.build_team`, imported lazily); the
  benchmark depends only on the frozen contracts in `agent/teams/types.py` plus `build_team`,
  `parse_team_spec` and `is_priced`.
- One rollout per task and team: results carry sampling noise that the intervals reflect only
  through the binomial model. Repeat with another `--seed` or `--instance-id` set to tighten them.
- Instances are LLM-curated, not human-verified; some tests are flaky or time-dependent. `gold`
  filters the obviously broken ones on the day of the run; remaining label noise lowers every
  team's rate equally.
- Fresh does not mean unseen by retrieval-augmented agents with web access.
- Image architecture is `linux/amd64`, as Modal requires.

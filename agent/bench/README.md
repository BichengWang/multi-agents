# Fresh-task benchmark

Runs team specs (`solo`, `review`, `best`, see [`docs/ROADMAP.md`](../../docs/ROADMAP.md)) on GitHub
issues that were opened *after* the models' training cutoffs, grades the resulting patches with the
dataset's official hidden-test logic in sandboxes (remote Modal sandboxes or containers on your own
Docker daemon), and reports team-vs-solo results: resolve rate with confidence intervals, paired
significance, cost and wall time.

```text
dataset.py   load + filter + seeded sample of tasks      sandbox.py   sandbox = Workspace; Modal backend
evaluate.py  official grading in a fresh sandbox          docker_backend.py  local Docker backend
run.py       task x team -> JSONL (resumable)             report.py    Markdown report, Wilson / McNemar
cli.py       python -m agent.bench ...
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

Deviations, all forced by running in a sandbox backend instead of RepoLaunch:

- Upstream drives containers through RepoLaunch's Docker runtime; we run the same commands via
  Modal Sandbox exec or `docker exec`. RepoLaunch itself (Python >= 3.12, LangChain stack, Docker
  SDK) is not installed; grading needs none of it. The decision logic is a line-for-line port.
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
    ANTHROPIC_API_KEY=... CODEX_API_KEY=... [OPENROUTER_API_KEY=...]
```

- **Modal Secret** `multi-agents-bench` (override with `--secret`) is injected into *agent*
  sandboxes only, never into grading sandboxes. `run` derives which keys the CLIs inside the
  sandbox need (`claude` -> `ANTHROPIC_API_KEY`, `codex` -> `CODEX_API_KEY`, an OpenAI API key:
  without a saved login `codex exec` reads only that variable and ignores `OPENAI_API_KEY`, see
  [non-interactive mode](https://developers.openai.com/codex/noninteractive)) and Modal fails fast
  naming any that are missing. `sdk` harnesses call their provider from the host process, so their
  keys (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, ...) come from your host environment and are never
  put in a sandbox. Teams run with permissions bypassed inside the sandbox
  (`build_team(spec, permissions="bypass")`; Claude Code only accepts `--dangerously-skip-permissions` as root when `IS_SANDBOX=1`, which the sandbox sets, see [this write-up](https://wmedia.es/en/tips/claude-code-dangerously-skip-permissions-what-it-does); Codex gets [`--dangerously-bypass-approvals-and-sandbox`](https://developers.openai.com/codex/cli/reference)), so
  **an agent can read the keys in its sandbox: use keys with a spend limit.** Everything the run
  writes (`results.jsonl`, `artifacts/`, `gold.jsonl`) is scrubbed first: the values of every
  credential-looking variable (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`) read from the agent
  sandbox's own environment and from your host environment, plus anything shaped like a provider
  key (`sk-...`, `sk-ant-...`, `ghp_...`, `AIza...`, ...), becomes `[REDACTED]`. Records say whether
  that happened (`redacted`, `patch_redacted`). Grading uses the unscrubbed patch. The scrub is
  pattern- and value-based, so skim `artifacts/` before publishing trajectories.
- Images are pulled from Docker Hub by Modal. If pulls are rate limited, create a secret with
  `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` and pass `--registry-secret NAME`.
- Agent sandboxes extend the instance image with Node.js 24.21.0 (tarball checksum-verified against
  `SHASUMS256.txt`), `@anthropic-ai/claude-code@2.1.287` and `@openai/codex@0.162.1` (pins in
  `sandbox.py`). The layer is cached by Modal per instance image; the first run pays the build.
  Instance images must be glibc-based (Debian/Ubuntu-style; the Node tarball is glibc).
- Each sandbox gets 2 Modal cores (= the dataset's recommended 4 CPUs) and 16 GB; a few large C++
  repositories need more (upstream mentions up to 50 GB).

### Local Docker instead of Modal

`--backend docker` (on `gold` and `run`) runs the same images, setup, diff and grading on your own
Docker daemon; no Modal account is needed.

```bash
export ANTHROPIC_API_KEY=... CODEX_API_KEY=...       # only the keys your teams need
python -m agent.bench gold --backend docker --since 2026-07-01 --limit 10 --out runs/gold-local
```

- API keys come from your environment: `run` names any missing key before starting. They are
  passed into agent containers by name (not on the command line), never into grading containers.
- Agent images are built once per instance image (`docker run` + `docker commit`, no builder plugin)
  and kept as `multi-agents-bench-agent:<hash>`. Images are pulled with your `docker login`.
- Containers get 4 CPUs and 16 GB as limits (`--cpus` / `--memory`), run `sleep` as PID 1 with
  `--rm`, and carry the label `multi-agents-bench=1`. A timed-out command is killed with every
  other process in its container (PID 1 excepted).
- Instance images are `linux/amd64`. On Apple silicon run the daemon with Rosetta
  (`colima start --vm-type vz --vz-rosetta`, which needs `softwareupdate --install-rosetta`);
  under QEMU emulation builds are several times slower and timing-sensitive tests fail.
  Validate gold on the machine you run teams on, and do not compare wall times across backends
  (the report names the backend).
- Disk: instance images are 1 to 5 GB each and agent images add about 1.3 GB.

## Commands

```bash
# 1. No credentials needed: the seeded sample of fresh tasks (downloads the pinned parquet files).
python -m agent.bench tasks --since 2026-07-01 --limit 50 --seed 7

# 2. Keep only tasks whose gold patch still resolves (sandboxes, no model cost).
python -m agent.bench gold --since 2026-07-01 --out runs/gold --concurrency 8

# 3. Run teams (resumable; re-run the same command to continue).
python -m agent.bench run --since 2026-07-01 --limit 50 --seed 7 \
    --gold-file runs/gold/gold.jsonl \
    --team solo:worker=claude@claude-sonnet-5-5 \
    --team review:coder=claude@claude-sonnet-5-5,reviewer=claude@claude-sonnet-5-5 \
    --team best:workers=claude@claude-sonnet-5-5+claude@claude-sonnet-5-5+claude@claude-sonnet-5-5 \
    --max-cost-usd 600 --concurrency 6 --out runs/pilot

# 4. Report, comparing every team with the solo baseline on the same tasks.
python -m agent.bench report runs/pilot --baseline solo:worker=claude@claude-sonnet-5-5
```

`run` writes to `--out`: `results.jsonl` (one record per task x team: patch, resolved, usage and
cost, duration, runs summary, grading details, error), `meta.json`, and
`artifacts/<team>/<task>/{patch.diff,runs.json,eval.log}` with full harness logs (the raw
trajectories). Filters shared by `tasks`, `gold` and `run`: `--dataset`, `--since` (inclusive),
`--until` (exclusive), `--repo`, `--language`, `--instance-id`, `--limit`, `--seed`.
`meta.json` keeps the first invocation's `created_at` and appends one entry per invocation with the
complete filter set (dataset, dates, repos, languages, instance ids, limit, seed, gold file, history
and base-commit checks), the teams, the task ids and the budget; `report` covers the teams and tasks
of the latest entry.

Every record also notes `isolate_history` and `image_head`, the commit the image was checked out
at. Setup refuses an image whose checkout is not the task's `base_commit` (a dataset/image mismatch
would otherwise be graded silently); `--no-check-base-commit` turns that off.

- **Errors vs. timeouts.** Only infrastructure failures are *errored* pairs: the workspace could not
  be created (nothing was spent), the workspace could not be read after the team stopped, or
  grading failed. A team that exceeds `--solve-timeout`, or crashes, is **graded on whatever it left
  in the workspace** and recorded with `meta.timed_out` / `meta.solve_error`; those pairs stay in
  the statistics (dropping them would remove the hardest tasks for the slowest topologies from every
  team's numbers) and are shown in the report's *Timed out* column. Their cost is unknown.
- **Resume**: finished pairs (timeouts included) are skipped; errored pairs are retried
  (`--no-retry-errors` to keep them). An error in the *grading* stage re-grades the saved patch
  instead of re-running (and re-paying for) the agents, unless the stored patch had to be scrubbed.
  Before resuming, `run` and `gold` repair a final line torn by an interrupted write in
  `results.jsonl` and `gold.jsonl`: an unparseable final line without a newline is removed, and a
  valid final record without a newline gets one. Any newline-terminated line that is not valid JSON
  stops loading with its file path and line number. `report` skips a torn final line and leaves the
  file as is.
- **Budget** (`--max-cost-usd X`): no new pair starts once priced spend, including earlier runs in
  the same `--out`, reaches X; running pairs finish, so spend can exceed X by up to `--concurrency`
  pairs. A budgeted run refuses to start if a team contains a harness whose cost cannot be priced
  (e.g. `codex` without `@model`) unless `--allow-unpriced`. Without `--allow-unpriced` it also
  stops launching as soon as any record has an unknown cost, **which includes every timed-out or
  crashed solve** (the agents may have spent money the run cannot see; the halt survives a
  restart). Size `--solve-timeout` so timeouts are rare, or pass `--allow-unpriced` to accept that
  the budget then counts priced spend only. The budget covers model spend, not Modal compute.
- **Cancellation**: Ctrl-C cancels in-flight pairs and terminates their sandboxes.
- `--solve-timeout` (default 3600 s) bounds one team solve. Grading has one 150-minute budget for
  apply + rebuild + tests (upstream allows 150 minutes per command); a run that hits it is
  unresolved with `timed_out: true`. Modal's client swallows its own exec deadline and reports exit
  code -1, which `sandbox.py` turns into a timeout.
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
  before the agent starts (`git gc --prune=now` failing fails the pair, never silently leaks), so
  the fix commit cannot be read with `git log --all`. The hidden tests
  are only ever written into a separate grading sandbox, which has no secrets and no agent CLIs.
  What cannot be stripped: network access (an agent could in principle fetch the upstream fix; the
  protocol's trajectory-audit remedy applies, and `artifacts/` keeps the trajectories).
- **Patch** = everything changed since the sandbox started (commits, edits, new files,
  `.gitignore` honoured), diffed against a tree snapshot taken at start. Files that were untracked
  but not ignored in the image (build output, generated reports) are excluded from the snapshot
  and from every diff, so rebuild churn is not attributed to the agent and cannot crowd the real
  change out of the reviewer's and judge's prompts. New Python bytecode (`__pycache__/`, `*.pyc`)
  is left out too, even where the repository does not ignore it (`GENERATED_FILES` in
  `agent/teams/types.py`, shared with local worktrees).
- **Statistics** (`report.py`, pure Python): resolve rate with the Wilson 95 % interval; paired
  comparison against the baseline on the *same* tasks with the exact two-sided McNemar test on
  discordant tasks (wins = team solved, baseline did not; losses = the reverse); mean, median and
  per-resolved cost; median wall time. All statistics use the *common* tasks, those with a valid
  result for every team. Errored pairs (infrastructure failures only, see above) are excluded and
  listed by name; `--count-errors-as-failures` counts them as unresolved instead. Pairs that never
  ran (budget halt) are listed under *Not run* and also leave their task out of every team's
  numbers. `report` covers the teams and tasks of the latest `meta.json` entry (`--team` overrides).
  No
  multiplicity correction across teams is applied. With 50 tasks only large effects reach
  significance: 6 wins against 0 losses gives p = 0.031 (5 against 0 gives 0.063), 12 against 3
  gives 0.035; a 4 to 8 point gap is not detectable, so read the intervals.
- **Cost** is `TeamResult.usage` summed over all harness runs. Unpriced runs (and timed-out or
  crashed solves) have unknown cost. The mean then shows `≥` the sum of priced costs divided by *all*
  runs, which is a true lower bound; cost per resolved shows `≥` priced total over resolved runs;
  the median has no bound and is labelled "priced runs only".

## Cost estimate

Anchors, measured by others on a fresh-task benchmark (SWE-rebench leaderboard,
[swe-rebench.com](https://swe-rebench.com/leaderboard), window 2026-05-15 to 2026-07-01,
111 problems; read 2026-10-13): **Claude Code $3.39 per problem** (60.4 % resolved),
**Codex $1.59** (58.0 %), Sonnet 5 [high] $1.43, Opus 5 [high] $3.47, GPT-5.6 Sol [medium] $0.85,
GPT-5.6 Luna [medium] $0.11 (395,522 tokens per problem). Those are Python tasks at then-current
prices; this benchmark's tasks skew to Rust/Java/C/C++ and need larger builds, so treat every number
as an order of magnitude and replace it with measured cost from a 5-task run.

Prices used ([Anthropic models overview](https://platform.claude.com/docs/en/models/overview),
`agent/teams/pricing.py` from [OpenAI pricing](https://developers.openai.com/api/docs/pricing)):
Haiku 5.5 from $0.10 / $0.50 per MTok (the tiers above 100K prompt tokens, $0.50 / $2.50 with cache
reads $0.05, come from a [third-party comparison](https://platform.teamai.com/blog/large-language-models-llms/understanding-different-claude-models/), not Anthropic's page), Sonnet 5.5 $2 / $10, `gpt-6-luna` $0.10 input, $0.01 cached input, $0.50 output.

Modal sandbox rate (from [modal.com/pricing](https://modal.com/pricing), Sandbox rates):
2 cores x $0.00003942 + 16 GiB x $0.00000667 = $0.0001856/s = **$0.668/hour**. Grading alone
(15 minutes) is $0.167 per task.

Shared assumptions. Pair sandbox time: 20 minutes of agent work per worker plus 15 minutes of
grading. Multipliers on a solo run (assumptions, not measurements): a reviewer pass costs about 0.4x
of the coder run, a fix pass 0.3x, the judge 0.3x of one worker. `review` low end = coder plus one
approving review (1 + 0.4 = 1.4x, 43 minutes); high end = the default 3 rounds of review and fix
(1 + 3 x (0.4 + 0.3) = 3.1x, 75 minutes). `best` = 3 workers + judge = 3.3x, 3 x 20 + 10 + 15 = 85
minutes. Image builds (first use of each instance image) are not estimated.

### Recommended first pilot: about $467, within $500

Cheap models carry the 50-task comparison of all three topologies; a small frontier spot-check
follows. Per-task LLM costs for the cheap tier are deliberately rounded up: Codex with `gpt-6-luna`
$0.25 (SWE-rebench: GPT-5.6 Luna $0.11 bare, Codex-style agent overhead about 1.9x from $1.59 vs
$0.85, and `gpt-6-luna` is priced below `gpt-5.6-luna`), Claude Code with Haiku 5.5 $1.00 (Claude
Code measured $3.39, which we assume was at Sonnet-class prices; Haiku 5.5 at its long-context tier is a quarter of
Sonnet 5.5's list price, so $0.85, rounded up for weaker models taking more turns).

| Stage | LLM per task | Sandbox per pair | LLM total | Sandbox total | Total |
|---|---|---|---|---|---|
| gold validation, 70 candidate tasks | | 15 min = $0.167 | | $11.7 | **$12** |
| 50 tasks `solo:worker=codex@gpt-6-luna` | $0.25 | 35 min = $0.39 | $12.50 | $19.50 | **$32** |
| 50 tasks `review:coder=codex@gpt-6-luna,reviewer=codex@gpt-6-luna` (3 rounds) | 3.1 x $0.25 = $0.775 | 75 min = $0.835 | $38.75 | $41.75 | **$80.50** |
| 50 tasks `best:workers=codex@gpt-6-luna+codex@gpt-6-luna+codex@gpt-6-luna` | 3.3 x $0.25 = $0.825 | 85 min = $0.946 | $41.25 | $47.30 | **$88.55** |
| 50 tasks `solo:worker=claude@claude-haiku-5-5` (cross-harness reference) | $1.00 | 35 min = $0.39 | $50.00 | $19.50 | **$69.50** |
| *Stage 1 total* | | | *$142.50* | *$139.75* | ***$282*** |
| gold validation, 25 candidates for the spot-check | | $0.167 | | $4.2 | **$4** |
| 20 tasks `solo:worker=claude@claude-sonnet-5-5` | $3.39 | 35 min = $0.39 | $67.80 | $7.80 | **$75.60** |
| 20 tasks `review:coder=claude@claude-sonnet-5-5,reviewer=claude@claude-sonnet-5-5,rounds=1` | 1.4 x $3.39 = $4.75 | 43 min = $0.48 | $94.92 | $9.58 | **$104.50** |
| *Stage 2 total* | | | *$162.72* | *$21.58* | ***$184*** |
| **Both stages** | | | **$305** | **$161** | **$467** |

```bash
# Stage 1 (50 tasks, three topologies on the cheap tier)
python -m agent.bench gold --since 2026-07-01 --limit 70 --seed 7 --out runs/gold
python -m agent.bench run --since 2026-07-01 --limit 50 --seed 7 --gold-file runs/gold/gold.jsonl \
    --team solo:worker=codex@gpt-6-luna \
    --team review:coder=codex@gpt-6-luna,reviewer=codex@gpt-6-luna \
    --team best:workers=codex@gpt-6-luna+codex@gpt-6-luna+codex@gpt-6-luna \
    --team solo:worker=claude@claude-haiku-5-5 \
    --max-cost-usd 160 --solve-timeout 5400 --concurrency 6 --out runs/pilot-cheap
python -m agent.bench report runs/pilot-cheap --baseline solo:worker=codex@gpt-6-luna

# Stage 2 (frontier spot-check; a different 20-task draw, compared within itself)
python -m agent.bench gold --since 2026-07-01 --limit 25 --seed 8 --out runs/gold2
python -m agent.bench run --since 2026-07-01 --limit 20 --seed 8 --gold-file runs/gold2/gold.jsonl \
    --team solo:worker=claude@claude-sonnet-5-5 \
    --team review:coder=claude@claude-sonnet-5-5,reviewer=claude@claude-sonnet-5-5,rounds=1 \
    --max-cost-usd 180 --solve-timeout 5400 --concurrency 4 --out runs/pilot-frontier
```

`--max-cost-usd` bounds model spend only; the roughly $161 of Modal compute is on top, so also set a
spend limit in the Modal workspace settings. The estimate has little headroom ($33): a first
timeout halts a budgeted run (see Budget above), which is the intended brake. Run 5 tasks first
(`--limit 5 --max-cost-usd 15`), read the measured cost per task in `report`, and rescale before
committing to the full stages. Twenty tasks per arm only detect large differences (see Statistics).

### Full-scale option (not within a $500 budget): about $1,156 to $1,462

Frontier Claude Code in all configurations, 50 tasks each, same assumptions:

| Config (50 tasks) | LLM cost per task | Sandbox time per pair | LLM total | Sandbox total | Total |
|---|---|---|---|---|---|
| `solo` Claude Code | $3.39 | 35 min = $0.39 | $169.50 | $19.50 | **$189** |
| `solo` Codex | $1.59 | 35 min = $0.39 | $79.50 | $19.50 | **$99** |
| `review` Claude coder + Claude reviewer | 1.4x to 3.1x solo = $4.75 to $10.51 | 43 to 75 min = $0.48 to $0.84 | $237 to $525 | $24 to $42 | **$261 to $567** |
| `best` 3 Claude workers + judge | 3.3x solo = $11.19 | 85 min = $0.95 | $559 | $47 | **$607** |

The four configurations together cost roughly **$1,156 to $1,462**.

## Limitations

- **Live so far: local Docker only, gold and workspace paths.** On 2026-10-23, `gold --backend
  docker` graded `xroche__httrack-810` end to end (image pull, both patches applied, rebuild,
  175 tests parsed, `FAIL_TO_PASS` passed) in 6.5 minutes under QEMU on Apple silicon. Four
  timeout-sensitive `PASS_TO_PASS` tests failed (emulation is the likely cause; not checked on
  native amd64), so the task was marked invalid, which is what gold validation is for. A
  throwaway script exercised the agent workspace on the same image: agent image build with the
  pinned CLIs, history stripping, base-commit check, `diff`, `fork` + `apply`, a 5-second exec
  timeout and container removal. Not run yet: a team
  solving a task (needs API keys), and anything on real Modal. The Modal paths are covered by
  offline tests that drive the same code through a fake Modal client; its deadline behaviour (exit
  code -1, streams end, no exception) was read from the SDK source at tag `py/v1.6.0`, not
  observed. Unverified on Modal: whether it accepts every instance image without Python on `PATH`
  (use `--add-python 3.12` if not) and wall-clock cost per task.
- Teams come from the separate team runner (`agent.teams.build_team`, imported lazily); the
  benchmark depends only on the frozen contracts in `agent/teams/types.py` plus `build_team`,
  `parse_team_spec` and `is_priced`.
- One rollout per task and team: results carry sampling noise that the intervals reflect only
  through the binomial model. Repeat with another `--seed` or `--instance-id` set to tighten them.
- Instances are LLM-curated, not human-verified; some tests are flaky or time-dependent. `gold`
  filters the obviously broken ones on the day of the run; remaining label noise lowers every
  team's rate equally.
- Fresh does not mean unseen by retrieval-augmented agents with web access.
- Image architecture is `linux/amd64`, as Modal requires; local Docker on arm64 must emulate it.

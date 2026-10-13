# Multi-Agent Workflows and LLM Experiments

Python workflows built around the OpenAI Agents SDK: route a request to a story,
store-concept, or financial-research workflow; evaluate story workflows; and inspect
saved runs. Shared orchestration lives in [`agent/workflows`](agent/workflows/README.md).

The root project packages **only `agent/`**, with lightweight runtime dependencies.
Legacy chat, training, and serving experiments are isolated in [`ml/`](ml/) with
their own dependencies, lockfile, and virtual environment—not a single integrated
deployment pipeline.

## Setup

Run commands from the repository root unless noted otherwise. Python **3.10+** is
required by [`pyproject.toml`](pyproject.toml); the example below uses Python 3.11.

```bash
python3 -m pip install uv
uv venv --python 3.11
uv sync --locked --extra dev
source .venv/bin/activate
```

The root runtime depends on `openai-agents`, Pydantic, and Rich. The `dev` extra
adds pytest and Ruff; it does not install PyTorch, Transformers, Streamlit, or other
ML dependencies. `make bootstrap` also runs `uv sync --locked --extra dev`, using
the checked-in lockfile. Run `make lock` after editing dependencies, or
`make update-deps` to upgrade them intentionally.

### Credentials and model configuration

Export credentials in the shell that launches a live workflow; do not commit them:

```bash
export OPENAI_API_KEY="your_openai_api_key"
```

Agent workflows and live evaluations require access to the selected models/tools;
API calls incur costs. Role defaults and overrides live in
[`agent/config.py`](agent/config.py):

| Role | Default model | Environment override |
|---|---|---|
| Default | `gpt-5-mini` | `AGENT_MODEL_DEFAULT` |
| Planner | `gpt-5-mini` | `AGENT_MODEL_PLANNER` |
| Writer | `gpt-5` | `AGENT_MODEL_WRITER` |
| Verifier | `gpt-5-mini` | `AGENT_MODEL_VERIFIER` |
| Judge | `gpt-5` | `AGENT_MODEL_JUDGE` |
| Router | `gpt-5-mini` | `AGENT_MODEL_ROUTER` |

Legacy ML credentials and configuration belong to the separate `ml/` project.

## Agent workflows

After setup and exporting `OPENAI_API_KEY`:

```bash
# Let triage select story, store, or financial.
python -m agent.main "A heist on the moon"

# Bypass triage for a specific workflow.
python -m agent.main --route store "A sustainable coffee shop for a university campus"
python -m agent.main --route financial "Analyze Apple's most recent quarter"

# Compare story modes, or steer revisions interactively.
python -m agent.story_agent_simple.main --mode best-of-n -n 3 "A heist on the moon"
python -m agent.main --route story --story-checkpoint "A heist on the moon"
```

| Route | Workflow | Details |
|---|---|---|
| Story | Generate/evaluate once, refine with feedback (default), or keep the highest-scoring best-of-n candidate | [Story guide](agent/story_agent_simple/README.md) |
| Store | Generate → explain → evaluate → plan | [Store guide](agent/store_agent/README.md) |
| Financial | Plan web searches → research → write with optional specialist analysts → verify | [Financial result contract](agent/workflows/README.md#financial-research) |

Triage stops on an unknown or low-confidence route unless you supply
`--fallback story`, `--fallback store`, or `--fallback financial`. The default
minimum confidence is 0.5; `--min-confidence` changes it. Omitting the query prompts
for input.

Checkpoints show rejected story drafts before another revision: Enter continues,
text steers the next draft, and `/stop` returns the best-scoring draft so far.
The dedicated story CLI exposes this as `--checkpoint` in refine mode.

All three routes use an injectable runner and return `WorkflowResult`. Financial
results include the structured report, verification findings, search evidence, and
any fundamentals/risk analyst tool calls; those nested calls use the shared runner
too. Search or analyst failures propagate rather than silently dropping evidence.
A verifier's rejection remains visible in the result—it does not guarantee that
the report is factually correct.

Best-of-n candidates and financial searches use bounded concurrency, defaulting to
three workers. Configure `BestOfN(max_concurrency=...)` or
`FinancialResearchManager(max_concurrency=...)` in Python. Fan-out cancels and awaits
workers on propagated failure or cancellation; ordinary best-of-n candidate errors
are recorded, and remaining successful candidates stay eligible.
See [execution boundaries](agent/workflows/README.md#execution-boundaries).

## Tracing and saved runs

The shared `python -m agent.main` CLI wraps each request in an SDK trace and saves
a JSON run record under `runs/` after a successful run. Use `--runs-dir` to change
the location or `--no-save` to disable local saving; the latter does not disable
SDK tracing.

Records contain outputs, metadata, step inputs/outputs, per-call timings, and
tracked token totals for **every route**, including financial research and its
analyst calls. `RunTracker` adds a tracing span per agent call. The dedicated story
CLI prints its result but does not perform the shared CLI's JSON saving.

Run records include prompts and generated content; review them before sharing.
See [run artifacts](agent/workflows/README.md#run-artifacts) for programmatic usage
and the [SDK tracing reference](https://openai.github.io/openai-agents-python/ref/tracing/).

## Evaluation and development

```bash
# Live evaluation: requires API access, including when using --no-judge.
python -m agent.agents_eval.run --workflow story_refine \
  --dataset agent/agents_eval/datasets/story.jsonl

# Offline checks: no API keys needed.
make check

# Focused checks.
make test-workflows
make lint
make test FILE_NAME=test_eval_harness.py TEST_NAME=concurrency
```

The evaluation CLI registers `story_single`, `story_refine`, and `story_best_of_n`.
It writes reports under `agent/agents_eval/results/`; `--baseline` compares against
an existing report. `--no-judge` skips only the LLM judge, not story generation.
See the [async evaluation guide](agent/agents_eval/README.md#async-workflow-evals-recommended).

`make test` runs lint and the root offline test suite using the locked project
environment; shell activation is optional. `make check` also runs the offline
GitHub workflow and attribution tests and requires Node.js 22+. CI runs the same
Python checks on Python 3.10 and 3.11. `make clean` explicitly removes project
caches and reports without traversing virtual environments. These checks do not
run the legacy ML tests. See the [Makefile](Makefile) for targets and
[docs/ROADMAP.md](docs/ROADMAP.md) for workflow plans.

## Legacy ML experiments

[`ml/pyproject.toml`](ml/pyproject.toml) and [`ml/uv.lock`](ml/uv.lock) define a
separate, non-packaged project. Run its commands from `ml/`; `uv run` manages
`ml/.venv` without adding ML dependencies to the root environment. Its `train`
and `serve` extras conflict, so select one at a time.

```bash
cd ml
# GPT-2 / Wikitext training; downloads models/data and requires compute.
uv run --extra train python trainer/train.py

# Local FastAPI server; run after training has produced trainer/output/.
uv run --extra serve python server/serve.py
```

Training and the local server both use `./trainer/output` relative to `ml/`.
The server exposes `POST /chat` on port 8000; it is distinct from the Qwen chat
backend. Keep `WANDB_API_KEY` unset for training without W&B reporting.

For chat setup, start with the [ML chat guide](ml/README_chat_app.md); Modal
background is in the [deployment guide](ml/MODAL_DEPLOYMENT_GUIDE.md). These are
legacy experiments: check current source and SDK compatibility before deploying,
and set `MODAL_APP_URL` to an endpoint you control rather than the checked-in URL.
The [`ml/chat_app_qwen.py`](ml/chat_app_qwen.py) client posts directly to that URL;
the [`ml/web/chat_app_qwen.py`](ml/web/chat_app_qwen.py) variant appends `/generate`.
Match the backend contract to the client you choose.

## License

[MIT](LICENSE). Generated stories, business plans, and financial reports need human
review; evaluation scores do not establish real-world correctness.

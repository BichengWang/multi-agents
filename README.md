# Multi-Agents

Multi-agent workflows built on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/), with a shared core of orchestration patterns and an eval harness to measure them. The repo also has LLM training, serving and chat-app code (see [Other components](#other-components)).

## Architecture

```
agent/
├── config.py                  # model_for(role): model per role, overridable via AGENT_MODEL_<ROLE>
├── core/                      # shared building blocks
│   ├── schemas.py             # Evaluation: score 1-10, passed, strengths, issues, suggestions
│   ├── runner.py              # run_agent(): timeout + retries around Runner.run
│   └── patterns.py            # pipeline, fan_out, refine_loop
├── financial_research_agent/  # plan -> parallel web search -> writer (analysts as tools) -> verifier
├── story_agent_simple/        # refine_loop: generate -> evaluate -> revise
├── store_agent/               # pipeline: generate -> explain -> evaluate -> plan
├── fix_agents/financial/      # triage agent with handoffs to specialist agents
└── agents_eval/               # async eval harness + CLI (and the legacy sync framework)
```

### Orchestration patterns (`agent.core`)

| Pattern | Use it when | Example |
|---|---|---|
| `pipeline(steps, input)` | Each agent refines the previous agent's output | `store_agent` |
| `fan_out(agent, inputs)` | The same work runs over many independent inputs | same shape as the web searches in `financial_research_agent` |
| `refine_loop(generator, evaluator, task)` | Quality matters more than latency; an evaluator can judge the output | `story_agent_simple` |
| Handoffs (`Agent(handoffs=[...])`) | A router decides which specialist should answer | `fix_agents/financial` |
| Agents as tools (`agent.as_tool()`) | One agent should call specialists mid-task | financial writer → fundamentals/risk analysts |

Evaluator agents use `output_type=Evaluation`, so workflows can branch on the score instead of parsing free text.

### Adding a workflow

1. Create `agent/<name>/my_agents/*.py`, one `Agent` per file, with `model=model_for("<role>")` and an `output_type` wherever a later step reads the result.
2. Compose them in `agent/<name>/manager.py` with a pattern from `agent.core`.
3. Add `agent/<name>/main.py` and run it with `python -m agent.<name>.main`.
4. Add the module to `tests/test_agent_imports.py`, then register it in `agent/agents_eval/run.py` with a JSONL dataset to measure it.

### Running

```bash
export OPENAI_API_KEY=...
python -m agent.story_agent_simple.main
python -m agent.store_agent.main
python -m agent.financial_research_agent.main
```

### Evaluating

```bash
python -m agent.agents_eval.run --workflow story_single --dataset agent/agents_eval/datasets/story.jsonl
python -m agent.agents_eval.run --workflow story_refine --dataset agent/agents_eval/datasets/story.jsonl \
    --baseline agent/agents_eval/results/story_single_<timestamp>.json
```

See [agent/agents_eval/README.md](agent/agents_eval/README.md).

### Testing

`make test` runs ruff and the offline test suite. No API calls are made, and CI runs the same checks on every PR. Scripts that call live model APIs are in `scripts/llm_smoke/`.

## Other components

```
trainer/   # fine-tuning script
eval/      # LLM eval scripts
server/    # FastAPI model server
serve/     # Modal LLM serving
web/       # chat apps (Streamlit / Flask)
```

## Setup

1. Install `uv`:
```bash
pip install uv
```

2. Create a virtual environment and install dependencies:
```bash
# Agents + dev tools (pytest, ruff)
uv pip install -e '.[dev]'
# The train and serve extras conflict, so install one at a time:
uv pip install -e '.[train]'   # or '.[serve]'
```

## Training

To train the model:

```bash
cd trainer
python train.py
```

The training script will:
- Load a pre-trained GPT-3 model
- Fine-tune it on the Wikitext dataset
- Save the model to the `output` directory

## Serving

To serve the trained model:

```bash
cd server
python serve.py
```

The server will:
- Load the trained model from the `output` directory
- Start a FastAPI server on port 8000
- Provide a `/chat` endpoint for generating responses

## API Usage

Send a POST request to `http://localhost:8000/chat` with the following JSON body:
```json
{
    "messages": ["Hello, how are you?"],
    "max_length": 100,
    "temperature": 0.7
}
```

## Development

- To install development dependencies (pytest, ruff):
```bash
uv pip install -e .[dev]
```

- To update dependencies:
```bash
uv pip compile pyproject.toml -o requirements.txt
```

## Tracing
`financial_research_agent` emits OpenAI traces (a link is printed per run). See https://openai.github.io/openai-agents-python/tracing/

## Environment Variables

```
brew install direnv
direnv allow
```

Create a `.env` file in the root directory with the following variables:
```
MODEL_PATH=./output
MAX_LENGTH=100
TEMPERATURE=0.7
```

## Git Aliases (Optional)

```shell
git config --global alias.co checkout
git config --global alias.br branch
git config --global alias.ci commit
git config --global alias.st status
git config --global alias.ll "log --oneline"
git config --global alias.lg "log --oneline --graph --all --decorate"
git config --global alias.rb "pull --rebase origin"
git config --global alias.sq "rebase -i HEAD~10"
git config --global alias.dl '!git branch -D $1 && git push --delete origin $1'
git config --global push.default current
git config --global core.editor "vim"
git config --global alias.amendpush '!git add . && git commit --amend --no-edit && git push --force origin'
git config --global alias.pr '!f() { git add . && git commit -am "$1" && git rebase origin/master && git push origin && gh pr create --title "$1" --body ""; }; f'
```

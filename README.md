# Multi-Agent Workspace

Multi-agent workflows using `uv` for dependency management. The runtime package contains only
`agent/`; the legacy model training, serving, and chat apps have their own project in `ml/`.
## Project Structure
```
multi-agents/
├── agent/             # Multi-agent systems (see agent/workflows for shared patterns)
│   ├── workflows/     #   SequentialWorkflow, RefineLoop, BestOfN
│   ├── story_agent_simple/  # story generator + evaluator (single / refine / best-of-n)
│   ├── store_agent/   #   store concept pipeline: generate → explain → evaluate → plan
│   ├── financial_research_agent/
│   └── agents_eval/   #   eval harness: python -m agent.agents_eval.run
├── ml/                # Independent legacy ML project (own pyproject.toml and uv.lock)
│   ├── trainer/       #   Training scripts and configuration
│   ├── eval/          #   Legacy evaluation scripts
│   ├── server/        #   FastAPI server for model serving
│   ├── serve/         #   Modal model serving
│   ├── web/           #   Chat applications
│   ├── config/        #   Legacy configuration
│   └── docs/          #   Chat app reference documents
├── docs/ROADMAP.md    # Workflow roadmap
├── pyproject.toml     # Runtime dependencies and configuration
└── README.md          # Documentation
```

## Setup

1. Install `uv`:
```bash
pip install uv
```

2. Create a virtual environment and install dependencies:
```bash
uv venv --python 3.11  # Python 3.10+ required
source .venv/bin/activate
# Runtime + dev tools (pytest, ruff); no model training dependencies
uv pip install -e '.[dev]'
```

The root wheel ships only `agent`. Install legacy ML dependencies separately from `ml/`;
its `train` and `serve` extras conflict, so select one at a time. `uv run` creates `ml/.venv`
without adding these dependencies to the runtime environment.

## Training

To train the model:

```bash
cd ml
uv run --extra train python trainer/train.py
```

The training script will:
- Load a pre-trained GPT-2 model
- Fine-tune it on the Wikitext dataset
- Save the model to the `ml/trainer/output` directory

## Serving

To serve the trained model:

```bash
cd ml
uv run --extra serve python server/serve.py
```

The server will:
- Load the trained model from the `ml/trainer/output` directory
- Start a FastAPI server on port 8000
- Provide a `/chat` endpoint for generating responses

For the legacy chat apps, see [ml/README_chat_app.md](ml/README_chat_app.md) and
[ml/MODAL_DEPLOYMENT_GUIDE.md](ml/MODAL_DEPLOYMENT_GUIDE.md). Run their commands from `ml/`.

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

- To install development dependencies:
```bash
uv pip install -e '.[dev]'
```

- To update dependencies:
```bash
uv lock --upgrade
```

- To run the full offline suite:
```bash
make test
```

## Multi-Agent Workflows

Shared orchestration patterns (sequential, refine loop, best-of-n) live in
[`agent/workflows`](agent/workflows/README.md); the story agent is the reference user:

```bash
python -m agent.main "A heist on the moon"   # triage agent picks story / store / financial
python -m agent.story_agent_simple.main --mode refine "A heist on the moon"
python -m agent.main --route story --story-checkpoint "A heist on the moon"
make test-workflows  # offline tests, no API keys
```

Use `--story-checkpoint` to inspect rejected story drafts before another revision: press Enter to
continue, type feedback to steer the next draft, or enter `/stop` to finish with the best draft.
The dedicated story entrypoint exposes the same option as `--checkpoint` in refine mode.

Planned next steps: [docs/ROADMAP.md](docs/ROADMAP.md).

## Tracing
TBD, https://openai.github.io/openai-agents-python/ref/tracing/

## Environment Variables

```
brew install direnv
direnv allow
```

Set the API key in your environment before running a workflow:
```bash
export OPENAI_API_KEY=your_openai_api_key_here
```

Legacy chat and training settings are documented under `ml/`.

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

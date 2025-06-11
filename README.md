# Online Chatbot Monorepo

A monorepo for training and serving LLM models using `uv` for dependency management.

## Project Structure

```
online-chat/
├── trainer/           # Training scripts and configuration
├── server/            # FastAPI server for model serving
├── client/            # Client applications
├── pyproject.toml     # Project dependencies and configuration
└── README.md          # Documentation
```

## Setup

Run the following command to set up the project:

```bash
make setup
```

This will:
1. Install `uv` package manager
2. Create a virtual environment
3. Install project dependencies

After setup, activate the virtual environment:
```bash
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

## Training

To train the model:

```bash
make train
```

The training script will:
- Load a pre-trained GPT-2 model
- Fine-tune it on the Wikitext dataset
- Save the model to the `output` directory

## Serving

To serve the trained model:

```bash
make serve
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

- To install development dependencies:
```bash
make install-dev
```

- To update dependencies:
```bash
make update-deps
```

## Environment Variables

Create a `.env` file in the root directory with the following variables:

```
MODEL_PATH=./output
MAX_LENGTH=100
TEMPERATURE=0.7
```

## Optional

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
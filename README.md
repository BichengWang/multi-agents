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

1. Install `uv`:
```bash
pip install uv
```

2. Create a virtual environment and install dependencies:
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
uv pip install -e .[train,serve]
```

## Training

To train the model:

```bash
cd trainer
python train.py
```

The training script will:
- Load a pre-trained GPT-2 model
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

- To install development dependencies:
```bash
uv pip install -e .[dev]
```

- To update dependencies:
```bash
uv pip compile pyproject.toml -o requirements.txt
```

## Environment Variables

Create a `.env` file in the root directory with the following variables:

```
MODEL_PATH=./output
MAX_LENGTH=100
TEMPERATURE=0.7
```

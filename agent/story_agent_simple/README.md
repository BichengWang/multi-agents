# Simple Story Agent Workflow

This module implements a simplified multi-agent workflow for generating and evaluating story concepts, inspired by the original story agent architecture but streamlined to just two agents.

## Agent Chain

- **Story Generator Agent**: Creates creative story concepts and narratives.
- **Story Evaluator Agent**: Critically evaluates the story for quality, coherence, and potential.

## Usage

```bash
python -m agent.story_agent_simple.main "A sci-fi story about time travel and redemption"
```

Omit the query to be prompted for it. Choose an orchestration mode with `--mode`:

| Mode | What happens |
|---|---|
| `single` | Generate once, evaluate once (the original two-step flow). |
| `refine` (default) | Generate → evaluate → revise with the evaluator's feedback, until it passes or `--max-iterations` (default 3). `--threshold 8` accepts on score instead of the evaluator's `passed` flag. |
| `best-of-n` | Generate `-n` (default 3) candidates in parallel, each from a different angle, and keep the highest-scoring one. |

The evaluator returns a structured `StoryEvaluation` (overall `score`, `passed`, `feedback`, and
per-dimension scores), which is what lets the loop decide when to stop. The modes are built on the
shared patterns in [`agent/workflows`](../workflows/README.md).

## Architecture

- **StoryGeneratorAgent**: Defined in `my_agents/generator_agent.py` - creates innovative story concepts
- **StoryEvaluatorAgent**: Defined in `my_agents/evaluator_agent.py` - scores story quality with structured `StoryEvaluation` output
- **SimpleStoryManager**: Picks the workflow for the chosen mode in `manager.py`
- **main.py**: Entry point for the application

## Example Query

> "Create a science fiction story about time travel and redemption."

## Customization

You can modify the agent prompts in the `my_agents/` directory to adjust the focus of story generation and evaluation. The simple two-agent structure makes it easy to understand and extend for different storytelling domains. 
# Simple Story Agent Workflow

This module implements a simplified multi-agent workflow for generating and evaluating story concepts, inspired by the original story agent architecture but streamlined to just two agents.

## Agent Chain

- **Story Generator Agent**: Creates creative story concepts and narratives.
- **Story Evaluator Agent**: Critically evaluates the story for quality, coherence, and potential.

## Usage

Run the workflow interactively:

```bash
python -m agents.story_agent_simple.main
```

You will be prompted to enter a story concept or idea. The agents will process the idea in sequence:
1. The Story Generator will create a detailed story concept
2. The Story Evaluator will provide a comprehensive evaluation

## Architecture

- **StoryGeneratorAgent**: Defined in `my_agents/generator_agent.py` - creates innovative story concepts
- **StoryEvaluatorAgent**: Defined in `my_agents/evaluator_agent.py` - evaluates story quality and potential
- **SimpleStoryManager**: Orchestrates the workflow in `manager.py`
- **main.py**: Entry point for the application

## Example Query

> "Create a science fiction story about time travel and redemption."

## Customization

You can modify the agent prompts in the `my_agents/` directory to adjust the focus of story generation and evaluation. The simple two-agent structure makes it easy to understand and extend for different storytelling domains. 
# Store Agent Workflow

This module implements a multi-agent workflow for generating, explaining, evaluating, and managing new store or business concepts, inspired by the financial research agent architecture.

## Agent Chain

- **Store Generator Agent**: Proposes creative store/business ideas.
- **Store Explainer Agent**: Breaks down and explains the business model and operations.
- **Store Evaluator Agent**: Critically evaluates the concept for viability, risk, and opportunity.
- **Store Manager Agent**: Produces an actionable implementation/management plan.
- **Store Coordinator Agent**: (Optional) Orchestrates the full workflow and synthesizes results.

## Usage

Run the workflow interactively:

```bash
python -m agents.story_agent.main
```

You will be prompted to enter a store concept or business idea. The agents will process the idea in sequence, and you will see the generated concept, explanation, evaluation, and management plan.

## Architecture

- Each agent is defined in `agents/` as an `Agent` with a specific prompt.
- The workflow is orchestrated by `StoreAgentManager` in `manager.py`.
- The entrypoint is `main.py`.

## Example Query

> "Create a sustainable coffee shop concept for a university campus."

## Customization

You can modify the agent prompts or add new agents in the `agents/` directory to extend the workflow for other business domains. 
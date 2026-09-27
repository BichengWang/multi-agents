from agent.core import pipeline

from .my_agents.evaluator_agent import evaluator_agent
from .my_agents.explainer_agent import explainer_agent
from .my_agents.generator_agent import generator_agent
from .my_agents.manager_agent import manager_agent

STEPS = [
    ("Generated Concept", generator_agent),
    ("Explanation", explainer_agent),
    ("Evaluation", evaluator_agent),
    ("Management Plan", manager_agent),
]


class StoreAgentManager:
    """
    Orchestrates the full flow: generation, explanation, evaluation, and management of store concepts.
    """

    async def run(self, query: str):
        print("Starting store agent workflow...")
        outputs = await pipeline([agent for _, agent in STEPS], query)
        for (title, _), output in zip(STEPS, outputs):
            print(f"\n{title}:\n{output}")
        print("\nStore agent workflow complete.")
        return outputs

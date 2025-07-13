import asyncio
from agents import Runner

from .agents.generator_agent import generator_agent
from .agents.explainer_agent import explainer_agent
from .agents.evaluator_agent import evaluator_agent
from .agents.manager_agent import manager_agent
from .agents.coordinator_agent import coordinator_agent

class StoreAgentManager:
    """
    Orchestrates the full flow: generation, explanation, evaluation, and management of store concepts.
    """
    def __init__(self):
        pass

    async def run(self, query: str):
        print("Starting store agent workflow...")
        # Step 1: Generate store concept
        print("Generating store concept...")
        gen_result = await Runner.run(generator_agent, query)
        print(f"\nGenerated Concept:\n{gen_result.final_output}")

        # Step 2: Explain store concept
        print("\nExplaining store concept...")
        exp_result = await Runner.run(explainer_agent, gen_result.final_output)
        print(f"\nExplanation:\n{exp_result.final_output}")

        # Step 3: Evaluate store concept
        print("\nEvaluating store concept...")
        eval_result = await Runner.run(evaluator_agent, exp_result.final_output)
        print(f"\nEvaluation:\n{eval_result.final_output}")

        # Step 4: Management plan
        print("\nCreating management plan...")
        mgr_result = await Runner.run(manager_agent, eval_result.final_output)
        print(f"\nManagement Plan:\n{mgr_result.final_output}")

        print("\nStore agent workflow complete.") 
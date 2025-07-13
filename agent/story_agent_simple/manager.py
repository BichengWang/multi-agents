import asyncio
from agents import Runner

from .my_agents.generator_agent import generator_agent
from .my_agents.evaluator_agent import evaluator_agent

class SimpleStoryManager:
    """
    Orchestrates the simple story workflow: generation and evaluation of story concepts.
    """
    def __init__(self):
        pass

    async def run(self, query: str):
        print("Starting simple story agent workflow...")
        
        # Step 1: Generate story concept
        print("Generating story concept...")
        gen_result = await Runner.run(generator_agent, query)
        print(f"\nGenerated Story:\n{gen_result.final_output}")

        # Step 2: Evaluate story concept
        print("\nEvaluating story concept...")
        eval_result = await Runner.run(evaluator_agent, gen_result.final_output)
        print(f"\nEvaluation:\n{eval_result.final_output}")

        print("\nSimple story agent workflow complete.") 
from agent.core import refine_loop

from .my_agents.evaluator_agent import evaluator_agent
from .my_agents.generator_agent import generator_agent


class SimpleStoryManager:
    """
    Orchestrates the simple story workflow: generate a story, evaluate it, and revise
    with the evaluator's feedback until it passes or the round budget runs out.
    """

    def __init__(self, max_rounds: int = 3, threshold: int = 8):
        self.max_rounds = max_rounds
        self.threshold = threshold

    async def run(self, query: str):
        print("Starting simple story agent workflow...")
        result = await refine_loop(
            generator_agent,
            evaluator_agent,
            query,
            max_rounds=self.max_rounds,
            threshold=self.threshold,
        )

        for i, round_ in enumerate(result.rounds, start=1):
            evaluation = round_.evaluation
            print(f"\nRound {i}: score {evaluation.score}/10, passed={evaluation.passed}")
            for issue in evaluation.issues:
                print(f"  - {issue}")

        print(f"\nFinal Story (score {result.evaluation.score}/10):\n{result.output}")
        print("\nSimple story agent workflow complete.")
        return result

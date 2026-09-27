from agent.workflows import AgentRunner, SequentialWorkflow, Step, WorkflowResult, default_runner

from .my_agents.evaluator_agent import evaluator_agent
from .my_agents.explainer_agent import explainer_agent
from .my_agents.generator_agent import generator_agent
from .my_agents.manager_agent import manager_agent

STEP_TITLES = {
    "generate": "Generated Concept",
    "explain": "Explanation",
    "evaluate": "Evaluation",
    "plan": "Management Plan",
}


class StoreAgentManager:
    """
    Orchestrates the full flow: generation, explanation, evaluation, and management of store concepts.
    """

    def __init__(self, runner: AgentRunner = default_runner):
        self.runner = runner

    def build_workflow(self) -> SequentialWorkflow:
        return SequentialWorkflow(
            [
                Step("generate", generator_agent),
                Step("explain", explainer_agent),
                Step("evaluate", evaluator_agent),
                Step("plan", manager_agent),
            ],
            runner=self.runner,
        )

    async def run(self, query: str) -> WorkflowResult:
        print("Starting store agent workflow...")
        result = await self.build_workflow().run(query)
        for step in result.steps:
            print(f"\n{STEP_TITLES[step.name]}:\n{step.output}")
        print("\nStore agent workflow complete.")
        return result

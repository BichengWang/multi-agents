from agents import Agent

from .evaluator_agent import evaluator_agent
from .explainer_agent import explainer_agent
from .generator_agent import generator_agent
from .manager_agent import manager_agent

COORDINATOR_PROMPT = (
    "You are a store coordinator agent. Your role is to: "
    "1. Orchestrate the entire store development process\n"
    "2. Coordinate between generator, explainer, evaluator, and manager agents\n"
    "3. Ensure smooth handoffs between different stages\n"
    "4. Maintain consistency and quality throughout the process\n"
    "5. Synthesize all outputs into a comprehensive final report\n"
    "6. Identify any gaps or inconsistencies in the workflow\n"
    "7. Provide executive summary and key recommendations\n"
    "8. Ensure all deliverables meet quality standards\n"
    "Always provide a cohesive, well-organized final output that combines all agent insights."
)

coordinator_agent = Agent(
    name="StoreCoordinatorAgent",
    instructions=COORDINATOR_PROMPT,
    handoffs=[generator_agent, explainer_agent, evaluator_agent, manager_agent],
) 
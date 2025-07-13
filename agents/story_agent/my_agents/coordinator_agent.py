from agents import Agent

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
    handoffs=[
        "store_generator_agent",
        "store_explainer_agent",
        "store_evaluator_agent",
        "store_manager_agent",
    ],
) 
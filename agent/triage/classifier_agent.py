from agents import Agent

from agent.config import model_for
from agent.workflows import RouteDecision

CLASSIFIER_PROMPT = (
    "You are a triage agent for a multi-agent system. You receive a user request and a list of "
    "routes, each handled by a specialist workflow. Choose the single route whose description "
    "best matches what the user wants produced. Use the exact route name from the list. "
    "Set confidence below 0.5 when the request fits none of the routes well."
)

classifier_agent = Agent(
    name="TriageClassifierAgent",
    instructions=CLASSIFIER_PROMPT,
    model=model_for("router"),
    output_type=RouteDecision,
)

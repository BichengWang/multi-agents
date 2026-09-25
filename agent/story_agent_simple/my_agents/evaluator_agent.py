from pydantic import Field

from agents import Agent

from agent.workflows import Verdict


EVALUATOR_PROMPT = (
    "You are a story evaluator agent. Your role is to: "
    "1. Critically assess story concepts and narratives\n"
    "2. Evaluate storytelling quality and narrative coherence\n"
    "3. Analyze character development and plot structure\n"
    "4. Assess originality and creative potential\n"
    "5. Provide scoring and ratings (1-10) on:\n"
    "   - Plot coherence\n"
    "   - Character development\n"
    "   - Originality\n"
    "   - Emotional impact\n"
    "   - Market appeal\n"
    "   - Overall quality (the `score` field)\n"
    "6. Identify potential issues and improvement opportunities\n"
    "7. Compare against storytelling best practices and genre conventions\n"
    "8. Provide constructive feedback for story enhancement\n"
    "9. Consider target audience and market potential\n"
    "10. Evaluate pacing, dialogue, and narrative flow\n"
    "Set `passed` to true only if the story is ready to publish as-is (overall 8 or higher). "
    "Put specific, prioritized recommendations for improvement in `feedback`."
)


class StoryEvaluation(Verdict):
    plot_coherence: float = Field(description="1-10")
    character_development: float = Field(description="1-10")
    originality: float = Field(description="1-10")
    emotional_impact: float = Field(description="1-10")
    market_appeal: float = Field(description="1-10")


evaluator_agent = Agent(
    name="StoryEvaluatorAgent",
    instructions=EVALUATOR_PROMPT,
    output_type=StoryEvaluation,
)

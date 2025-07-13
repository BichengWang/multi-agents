# Import Agent from openai-agents-sdk and provide a simple Runner implementation

from typing import Any, Optional
from dataclasses import dataclass
from openai_agents_sdk import Agent

@dataclass
class RunResult:
    """Result from running an agent."""
    final_output: str
    input: str
    context: Optional[Any] = None

class Runner:
    """Simple Runner class for the multi-agents project."""
    
    @classmethod
    async def run(cls, agent: Agent, input_text: str, context: Optional[Any] = None) -> RunResult:
        """Run an agent with the given input and return the result."""
        # For now, just return a simple result
        # In a real implementation, this would call the agent's logic
        output = f"Agent '{agent.name}' processed: {input_text}"
        return RunResult(
            final_output=output,
            input=input_text,
            context=context
        )

__all__ = ['Agent', 'Runner', 'RunResult']

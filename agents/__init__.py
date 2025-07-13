# Simple Agent and Runner implementation for the multi-agents project

from typing import Any, Optional
from dataclasses import dataclass

@dataclass
class RunResult:
    """Result from running an agent."""
    final_output: str
    input: str
    context: Optional[Any] = None

class Agent:
    """Simple Agent class for the multi-agents project."""
    
    def __init__(self, name: str, instructions: str):
        self.name = name
        self.instructions = instructions

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

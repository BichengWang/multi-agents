from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass


@dataclass
class EvaluationResult:
    score: float
    feedback: str
    metrics: Dict[str, Any]
    raw_output: Any


class BaseEvaluator(ABC):
    """Base class for all evaluators in the framework."""
    
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.config = config or {}
    
    @abstractmethod
    def evaluate(self, agent_output: Any, context: Dict[str, Any]) -> EvaluationResult:
        """
        Evaluate the agent's output and return an EvaluationResult.
        
        Args:
            agent_output: The output from the agent to be evaluated
            context: Additional context information for evaluation
            
        Returns:
            EvaluationResult containing score, feedback, and metrics
        """
        pass
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert evaluator configuration to dictionary format."""
        return {
            "name": self.name,
            "config": self.config
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BaseEvaluator':
        """Create an evaluator instance from dictionary configuration."""
        return cls(name=data["name"], config=data.get("config")) 
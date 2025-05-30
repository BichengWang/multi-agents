from typing import Any, Dict, List, Optional, Type
from abc import ABC, abstractmethod
import importlib
import inspect


class AgentGenerator(ABC):
    """Base class for generating different types of agents."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.agent_registry: Dict[str, Type] = {}
    
    def register_agent(self, name: str, agent_class: Type) -> None:
        """Register a new agent type in the generator."""
        self.agent_registry[name] = agent_class
    
    def load_agent_from_module(self, module_path: str, class_name: str) -> None:
        """Dynamically load an agent class from a module."""
        try:
            module = importlib.import_module(module_path)
            agent_class = getattr(module, class_name)
            self.register_agent(class_name, agent_class)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Failed to load agent from {module_path}.{class_name}: {str(e)}")
    
    @abstractmethod
    def generate_agent(self, agent_type: str, **kwargs) -> Any:
        """
        Generate an agent instance of the specified type.
        
        Args:
            agent_type: The type of agent to generate
            **kwargs: Additional configuration parameters for the agent
            
        Returns:
            An instance of the requested agent type
        """
        pass
    
    def list_available_agents(self) -> List[str]:
        """Return a list of registered agent types."""
        return list(self.agent_registry.keys())


class DynamicAgentGenerator(AgentGenerator):
    """Implementation of AgentGenerator that supports dynamic agent creation."""
    
    def generate_agent(self, agent_type: str, **kwargs) -> Any:
        if agent_type not in self.agent_registry:
            raise ValueError(f"Unknown agent type: {agent_type}")
        
        agent_class = self.agent_registry[agent_type]
        agent_config = {**self.config, **kwargs}
        
        # Validate required parameters, ignore *args and **kwargs
        sig = inspect.signature(agent_class.__init__)
        missing_params = [
            name for name, param in sig.parameters.items()
            if name != 'self'
            and param.default == inspect.Parameter.empty
            and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and name not in agent_config
        ]
        
        if missing_params:
            raise ValueError(f"Missing required parameters for {agent_type}: {missing_params}")
        
        return agent_class(**agent_config) 
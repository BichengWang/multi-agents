from typing import Any, Dict, List, Optional, Type
from abc import ABC, abstractmethod
import importlib
import inspect
from .base_evaluator import BaseEvaluator


class EvaluatorGenerator(ABC):
    """Base class for generating different types of evaluators."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.evaluator_registry: Dict[str, Type[BaseEvaluator]] = {}
    
    def register_evaluator(self, name: str, evaluator_class: Type[BaseEvaluator]) -> None:
        """Register a new evaluator type in the generator."""
        if not issubclass(evaluator_class, BaseEvaluator):
            raise ValueError("Evaluator class must inherit from BaseEvaluator")
        self.evaluator_registry[name] = evaluator_class
    
    def load_evaluator_from_module(self, module_path: str, class_name: str) -> None:
        """Dynamically load an evaluator class from a module."""
        try:
            module = importlib.import_module(module_path)
            evaluator_class = getattr(module, class_name)
            self.register_evaluator(class_name, evaluator_class)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Failed to load evaluator from {module_path}.{class_name}: {str(e)}")
    
    @abstractmethod
    def generate_evaluator(self, evaluator_type: str, **kwargs) -> BaseEvaluator:
        """
        Generate an evaluator instance of the specified type.
        
        Args:
            evaluator_type: The type of evaluator to generate
            **kwargs: Additional configuration parameters for the evaluator
            
        Returns:
            An instance of the requested evaluator type
        """
        pass
    
    def list_available_evaluators(self) -> List[str]:
        """Return a list of registered evaluator types."""
        return list(self.evaluator_registry.keys())


class DynamicEvaluatorGenerator(EvaluatorGenerator):
    """Implementation of EvaluatorGenerator that supports dynamic evaluator creation."""
    
    def generate_evaluator(self, evaluator_type: str, **kwargs) -> BaseEvaluator:
        if evaluator_type not in self.evaluator_registry:
            raise ValueError(f"Unknown evaluator type: {evaluator_type}")
        
        evaluator_class = self.evaluator_registry[evaluator_type]
        evaluator_config = {**self.config, **kwargs}
        
        # Provide defaults for name and config if not supplied
        if 'name' not in evaluator_config:
            evaluator_config['name'] = evaluator_type
        if 'config' not in evaluator_config:
            evaluator_config['config'] = {}
        
        # Validate required parameters, ignore *args and **kwargs
        sig = inspect.signature(evaluator_class.__init__)
        missing_params = [
            name for name, param in sig.parameters.items()
            if name != 'self'
            and param.default == inspect.Parameter.empty
            and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and name not in evaluator_config
        ]
        
        if missing_params:
            raise ValueError(f"Missing required parameters for {evaluator_type}: {missing_params}")
        
        return evaluator_class(**evaluator_config) 
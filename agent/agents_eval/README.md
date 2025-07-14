# Agent Evaluation Framework

Ref: [Agent Eval @AmazonLab](https://github.com/awslabs/agent-evaluation/tree/main)

A flexible framework for evaluating multiple agents using multiple evaluators in a dynamic and configurable way.

## Features

- Dynamic agent generation and registration
- Dynamic evaluator generation and registration
- Configurable evaluation scenarios
- Comprehensive result tracking and metrics
- JSON-based result storage
- Detailed evaluation summaries

## Structure

The framework consists of the following components:

1. `BaseEvaluator`: Abstract base class for all evaluators
2. `AgentGenerator`: System for dynamic agent generation
3. `EvaluatorGenerator`: System for dynamic evaluator generation
4. `EvaluationFramework`: Main framework coordinating the evaluation process

## Usage

### 1. Create Your Agent

Your agent should implement a `run` method that accepts context parameters:

```python
class MyAgent:
    def __init__(self, name: str, **kwargs):
        self.name = name
    
    def run(self, **context) -> Any:
        # Your agent's logic here
        return result
```

### 2. Create Your Evaluator

Your evaluator should inherit from `BaseEvaluator` and implement the `evaluate` method:

```python
from agents_eval.base_evaluator import BaseEvaluator, EvaluationResult

class MyEvaluator(BaseEvaluator):
    def evaluate(self, agent_output: Any, context: Dict[str, Any]) -> EvaluationResult:
        # Your evaluation logic here
        return EvaluationResult(
            score=score,
            feedback=feedback,
            metrics=metrics,
            raw_output=agent_output
        )
```

### 3. Configure and Run Evaluation

```python
from agents_eval.evaluation_framework import EvaluationFramework, EvaluationConfig

config = EvaluationConfig(
    agent_configs=[
        {
            "type": "my_agent",
            "module_path": "path.to.my_agent",
            "class_name": "MyAgent",
            "params": {"name": "Agent1"}
        }
    ],
    evaluator_configs=[
        {
            "type": "my_evaluator",
            "module_path": "path.to.my_evaluator",
            "class_name": "MyEvaluator",
            "params": {}
        }
    ],
    context={"task": "my_task", "parameters": {}},
    output_dir="evaluation_results",
    experiment_name="my_experiment"
)

framework = EvaluationFramework(config)
framework.run_evaluation()
summary = framework.get_summary()
```

## Configuration

### Agent Configuration

Each agent configuration should include:
- `type`: Unique identifier for the agent type
- `module_path`: Python module path where the agent class is defined
- `class_name`: Name of the agent class
- `params`: Dictionary of parameters to pass to the agent constructor

### Evaluator Configuration

Each evaluator configuration should include:
- `type`: Unique identifier for the evaluator type
- `module_path`: Python module path where the evaluator class is defined
- `class_name`: Name of the evaluator class
- `params`: Dictionary of parameters to pass to the evaluator constructor

## Results

The framework generates two types of output:

1. **Detailed Results**: JSON files containing individual evaluation results for each agent-evaluator pair
2. **Summary**: Aggregated statistics including:
   - Total number of evaluations
   - Performance metrics per agent type
   - Statistics per evaluator type
   - Overall metrics (min, max, average scores)

## Example

See `example_usage.py` for a complete working example of the framework.

## Best Practices

1. **Agent Design**:
   - Make agents stateless when possible
   - Use clear input/output interfaces
   - Handle errors gracefully

2. **Evaluator Design**:
   - Focus on specific aspects of agent performance
   - Provide detailed feedback
   - Include relevant metrics

3. **Configuration**:
   - Use meaningful names for agent and evaluator types
   - Keep configuration modular and reusable
   - Document any special parameters

4. **Results Analysis**:
   - Review both individual results and summaries
   - Look for patterns in agent performance
   - Consider evaluator consistency 
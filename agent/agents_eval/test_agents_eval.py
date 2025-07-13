from typing import Any, Dict
from agents.agents_eval.evaluation_framework import EvaluationFramework, EvaluationConfig
from agents.agents_eval.base_evaluator import BaseEvaluator, EvaluationResult
from agents.agents_eval.agent_generator import DynamicAgentGenerator
from agents.agents_eval.evaluator_generator import DynamicEvaluatorGenerator


# Example agent class
class ExampleAgent:
    def __init__(self, name: str, **kwargs):
        self.name = name
    
    def run(self, **context) -> str:
        return f"Agent {self.name} completed task with context: {context}"


# Example evaluator class
class ExampleEvaluator(BaseEvaluator):
    def __init__(self, name: str = "example_evaluator", config=None, **kwargs):
        super().__init__(name=name, config=config)
    
    def evaluate(self, agent_output: str, context: Dict[str, Any]) -> EvaluationResult:
        # Simple evaluation logic
        score = 1.0 if "completed" in agent_output.lower() else 0.0
        feedback = "Good job!" if score > 0 else "Failed to complete task"
        
        return EvaluationResult(
            score=score,
            feedback=feedback,
            metrics={"completion_detected": score > 0},
            raw_output=agent_output
        )


def main():
    """
    python -m agents.agents_eval.test_agents_eval
    """
    # Create and configure generators
    agent_generator = DynamicAgentGenerator()
    evaluator_generator = DynamicEvaluatorGenerator()
    
    # Register agent and evaluator classes
    agent_generator.register_agent("example_agent", ExampleAgent)
    evaluator_generator.register_evaluator("example_evaluator", ExampleEvaluator)
    
    # Configure the evaluation
    config = EvaluationConfig(
        agent_configs=[
            {
                "type": "example_agent",
                "params": {"name": "Agent1"}
            },
            {
                "type": "example_agent",
                "params": {"name": "Agent2"}
            }
        ],
        evaluator_configs=[
            {
                "type": "example_evaluator",
                "params": {}
            }
        ],
        context={"task": "example_task", "parameters": {"param1": "value1"}},
        output_dir="agents/agents_eval/results",
        experiment_name="example_experiment"
    )
    
    # Create and run the evaluation framework
    framework = EvaluationFramework(config)
    framework.agent_generator = agent_generator
    framework.evaluator_generator = evaluator_generator
    framework.run_evaluation()
    
    # Print summary
    summary = framework.get_summary()
    print("\nEvaluation Summary:")
    print(f"Total evaluations: {summary['total_evaluations']}")
    print("\nAgent Performance:")
    for agent_type, stats in summary["agent_types"].items():
        print(f"\n{agent_type}:")
        print(f"  Count: {stats['count']}")
        print(f"  Average Score: {stats['avg_score']:.2f}")
    
    print("\nEvaluator Statistics:")
    for eval_type, stats in summary["evaluator_types"].items():
        print(f"\n{eval_type}:")
        print(f"  Count: {stats['count']}")
        print(f"  Average Score: {stats['avg_score']:.2f}")
    
    print("\nOverall Metrics:")
    metrics = summary["overall_metrics"]
    print(f"  Min Score: {metrics['min_score']:.2f}")
    print(f"  Max Score: {metrics['max_score']:.2f}")
    print(f"  Average Score: {metrics['avg_score']:.2f}")


if __name__ == "__main__":
    main() 
from typing import Any, Dict, List
from dataclasses import dataclass
import json
from datetime import datetime
from pathlib import Path

from .agent_generator import DynamicAgentGenerator
from .evaluator_generator import DynamicEvaluatorGenerator


@dataclass
class EvaluationConfig:
    """Configuration for the evaluation framework."""
    agent_configs: List[Dict[str, Any]]
    evaluator_configs: List[Dict[str, Any]]
    context: Dict[str, Any]
    output_dir: str
    experiment_name: str


class EvaluationFramework:
    """Main framework for coordinating agent and evaluator generation and execution."""
    
    def __init__(self, config: EvaluationConfig):
        self.config = config
        self.agent_generator = DynamicAgentGenerator()
        self.evaluator_generator = DynamicEvaluatorGenerator()
        self.results: List[Dict[str, Any]] = []
        
        # Create output directory
        self.output_dir = Path(config.output_dir) / config.experiment_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def load_agents_and_evaluators(self) -> None:
        """Load all agent and evaluator types from their respective modules."""
        for agent_config in self.config.agent_configs:
            if "module_path" in agent_config and "class_name" in agent_config:
                self.agent_generator.load_agent_from_module(
                    agent_config["module_path"],
                    agent_config["class_name"]
                )
        
        for evaluator_config in self.config.evaluator_configs:
            if "module_path" in evaluator_config and "class_name" in evaluator_config:
                self.evaluator_generator.load_evaluator_from_module(
                    evaluator_config["module_path"],
                    evaluator_config["class_name"]
                )
    
    def run_evaluation(self) -> List[Dict[str, Any]]:
        """
        Run the evaluation process for all agents with all evaluators.
        
        Returns:
            List of evaluation results for each agent-evaluator pair
        """
        self.load_agents_and_evaluators()
        
        for agent_config in self.config.agent_configs:
            agent = self.agent_generator.generate_agent(
                agent_config["type"],
                **agent_config.get("params", {})
            )
            
            agent_output = agent.run(**self.config.context)
            
            for evaluator_config in self.config.evaluator_configs:
                evaluator = self.evaluator_generator.generate_evaluator(
                    evaluator_config["type"],
                    **evaluator_config.get("params", {})
                )
                
                result = evaluator.evaluate(agent_output, self.config.context)
                
                evaluation_record = {
                    "timestamp": datetime.now().isoformat(),
                    "agent": {
                        "type": agent_config["type"],
                        "config": agent_config.get("params", {})
                    },
                    "evaluator": {
                        "type": evaluator_config["type"],
                        "config": evaluator_config.get("params", {})
                    },
                    "result": {
                        "score": result.score,
                        "feedback": result.feedback,
                        "metrics": result.metrics
                    }
                }
                
                self.results.append(evaluation_record)
                self._save_results()
        
        return self.results
    
    def _save_results(self) -> None:
        """Save the current evaluation results to a JSON file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = self.output_dir / f"evaluation_results_{timestamp}.json"
        
        with open(output_file, "w") as f:
            json.dump(self.results, f, indent=2)
    
    def get_summary(self) -> Dict[str, Any]:
        """
        Generate a summary of the evaluation results.
        
        Returns:
            Dictionary containing aggregated metrics and statistics
        """
        if not self.results:
            return {"error": "No evaluation results available"}
        
        summary = {
            "total_evaluations": len(self.results),
            "agent_types": {},
            "evaluator_types": {},
            "overall_metrics": {}
        }
        
        # Aggregate results by agent type
        for result in self.results:
            agent_type = result["agent"]["type"]
            evaluator_type = result["evaluator"]["type"]
            
            if agent_type not in summary["agent_types"]:
                summary["agent_types"][agent_type] = {
                    "count": 0,
                    "avg_score": 0,
                    "evaluations": []
                }
            
            if evaluator_type not in summary["evaluator_types"]:
                summary["evaluator_types"][evaluator_type] = {
                    "count": 0,
                    "avg_score": 0
                }
            
            # Update agent type statistics
            agent_stats = summary["agent_types"][agent_type]
            agent_stats["count"] += 1
            agent_stats["avg_score"] = (
                (agent_stats["avg_score"] * (agent_stats["count"] - 1) + result["result"]["score"])
                / agent_stats["count"]
            )
            agent_stats["evaluations"].append(result)
            
            # Update evaluator type statistics
            eval_stats = summary["evaluator_types"][evaluator_type]
            eval_stats["count"] += 1
            eval_stats["avg_score"] = (
                (eval_stats["avg_score"] * (eval_stats["count"] - 1) + result["result"]["score"])
                / eval_stats["count"]
            )
        
        # Calculate overall metrics
        all_scores = [r["result"]["score"] for r in self.results]
        summary["overall_metrics"] = {
            "min_score": min(all_scores),
            "max_score": max(all_scores),
            "avg_score": sum(all_scores) / len(all_scores)
        }
        
        return summary 
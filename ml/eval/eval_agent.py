import json
import random
import string
from typing import Any, Dict, Optional
from dataclasses import dataclass
import yaml
import html
from datetime import datetime, timedelta


@dataclass
class SyntheticDataConfig:
    num_items: int = 10
    min_value: int = 1
    max_value: int = 100
    include_timestamp: bool = True
    include_id: bool = True

    def __post_init__(self):
        """Validate configuration values after initialization."""
        if self.num_items < 0:
            raise ValueError("Number of items cannot be negative")
        if self.num_items > 10000:
            raise ValueError("Number of items cannot exceed 10000")
        if self.min_value >= self.max_value:
            raise ValueError("min_value must be less than max_value")
        if self.min_value < 0:
            raise ValueError("min_value cannot be negative")


class EvaluatorAgent:
    def __init__(self, config: Optional[SyntheticDataConfig] = None):
        self.config = config or SyntheticDataConfig()
        
    def _generate_random_string(self, length: int = 8) -> str:
        """Generate a random string of fixed length."""
        if length <= 0:
            raise ValueError("String length must be positive")
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    def _generate_timestamp(self) -> str:
        """Generate a random timestamp within the last 24 hours."""
        now = datetime.now()
        random_hours = random.randint(0, 24)
        random_minutes = random.randint(0, 60)
        timestamp = now - timedelta(hours=random_hours, minutes=random_minutes)
        return timestamp.isoformat()
    
    def _generate_synthetic_item(self) -> Dict[str, Any]:
        """Generate a single synthetic data item."""
        item = {
            "value": random.randint(self.config.min_value, self.config.max_value),
            "name": self._generate_random_string(),
            "category": random.choice(["A", "B", "C", "D"]),
        }
        
        if self.config.include_timestamp:
            item["timestamp"] = self._generate_timestamp()
        if self.config.include_id:
            item["id"] = self._generate_random_string(12)
            
        return item
    
    def generate_json(self) -> str:
        """Generate synthetic data in JSON format."""
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return json.dumps(data, indent=2)
    
    def generate_yaml(self) -> str:
        """Generate synthetic data in YAML format."""
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return yaml.dump(data, default_flow_style=False)
    
    def generate_html(self) -> str:
        """Generate synthetic data in HTML format."""
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        
        html_content = ["<table border='1'>"]
        # Add header
        if data:
            html_content.append("<tr>")
            for key in data[0].keys():
                html_content.append(f"<th>{html.escape(str(key))}</th>")
            html_content.append("</tr>")
        
        # Add data rows
        for item in data:
            html_content.append("<tr>")
            for value in item.values():
                html_content.append(f"<td>{html.escape(str(value))}</td>")
            html_content.append("</tr>")
        
        html_content.append("</table>")
        return "\n".join(html_content)
    
    def generate_python(self) -> str:
        """Generate synthetic data as Python code."""
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return f"data = {repr(data)}"
    
    def generate_data(self, format_type: str) -> str:
        """Generate synthetic data in the specified format."""
        if not isinstance(format_type, str):
            raise TypeError("Format type must be a string")
            
        format_type = format_type.lower()
        if format_type == "json":
            return self.generate_json()
        elif format_type == "yaml":
            return self.generate_yaml()
        elif format_type == "html":
            return self.generate_html()
        elif format_type == "python":
            return self.generate_python()
        else:
            raise ValueError(f"Unsupported format type: {format_type}")


def test_invalid_cases():
    """Test various invalid scenarios for the EvaluatorAgent."""
    print("\n=== Testing Invalid Cases ===")
    
    # Test 1: Invalid format type
    print("\nTest 1: Invalid format type")
    try:
        evaluator = EvaluatorAgent()
        evaluator.generate_data("invalid_format")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 2: Invalid configuration - negative number of items
    print("\nTest 2: Invalid configuration - negative number of items")
    try:
        config = SyntheticDataConfig(num_items=-5)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 3: Invalid configuration - min_value greater than max_value
    print("\nTest 3: Invalid configuration - min_value greater than max_value")
    try:
        config = SyntheticDataConfig(min_value=100, max_value=50)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 4: Empty data generation
    print("\nTest 4: Empty data generation")
    try:
        config = SyntheticDataConfig(num_items=0)
        evaluator = EvaluatorAgent(config)
        result = evaluator.generate_data("json")
        print(f"Empty data result: {result}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 5: Very large number of items
    print("\nTest 5: Very large number of items")
    try:
        config = SyntheticDataConfig(num_items=1000000)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 6: Invalid HTML content
    print("\nTest 6: Invalid HTML content")
    try:
        config = SyntheticDataConfig(num_items=1)
        evaluator = EvaluatorAgent(config)
        # Force an invalid HTML character
        evaluator._generate_random_string = lambda length: "<script>alert('xss')</script>"
        result = evaluator.generate_data("html")
        print(f"HTML with special characters: {result}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 7: Invalid format type (non-string)
    print("\nTest 7: Invalid format type (non-string)")
    try:
        evaluator = EvaluatorAgent()
        evaluator.generate_data(123)  # type: ignore
    except TypeError as e:
        print(f"Expected error: {e}")


if __name__ == "__main__":
    # Run valid test cases
    print("=== Running Valid Test Cases ===")
    config = SyntheticDataConfig(
        num_items=5,
        min_value=1,
        max_value=1000,
        include_timestamp=True,
        include_id=True
    )
    evaluator = EvaluatorAgent(config)
    
    print("\nJSON Output:")
    print(evaluator.generate_data("json"))
    print("\nYAML Output:")
    print(evaluator.generate_data("yaml"))
    print("\nHTML Output:")
    print(evaluator.generate_data("html"))
    print("\nPython Output:")
    print(evaluator.generate_data("python"))
    
    # Run invalid test cases
    test_invalid_cases()

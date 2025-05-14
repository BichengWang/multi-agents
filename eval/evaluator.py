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
        if length <= 0:
            raise ValueError("String length must be positive")
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    def _generate_timestamp(self) -> str:
        now = datetime.now()
        random_hours = random.randint(0, 24)
        random_minutes = random.randint(0, 60)
        timestamp = now - timedelta(hours=random_hours, minutes=random_minutes)
        return timestamp.isoformat()
    
    def _generate_synthetic_item(self) -> Dict[str, Any]:
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
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return json.dumps(data, indent=2)
    
    def generate_yaml(self) -> str:
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return yaml.dump(data, default_flow_style=False)
    
    def generate_html(self) -> str:
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        html_content = ["<table border='1'>"]
        if data:
            html_content.append("<tr>")
            for key in data[0].keys():
                html_content.append(f"<th>{html.escape(str(key))}</th>")
            html_content.append("</tr>")
        for item in data:
            html_content.append("<tr>")
            for value in item.values():
                html_content.append(f"<td>{html.escape(str(value))}</td>")
            html_content.append("</tr>")
        html_content.append("</table>")
        return "\n".join(html_content)
    
    def generate_python(self) -> str:
        data = [self._generate_synthetic_item() for _ in range(self.config.num_items)]
        return f"data = {repr(data)}"
    
    def generate_data(self, format_type: str) -> str:
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
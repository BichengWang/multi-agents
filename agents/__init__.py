# Import and re-export Agent and Runner from the openai-agents-python project
import sys
import os

# Add the other-repo/openai-agents-python/src directory to the Python path
other_repo_path = os.path.join(os.path.dirname(__file__), '..', '..', 'other-repo', 'openai-agents-python', 'src')
if other_repo_path not in sys.path:
    sys.path.insert(0, other_repo_path)

from agents import Agent, Runner

__all__ = ['Agent', 'Runner']

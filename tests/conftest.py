import os

# Agent modules may build OpenAI clients at import time; tests never call the API.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

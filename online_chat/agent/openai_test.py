from openai import OpenAI
import os

api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key)

response = client.responses.create(
    model="gpt-4.1",
    instructions="you're a story teller",
    input="Write a one-sentence bedtime story about a unicorn, use 2025 tendency story format",
    service_tier="auto",
    tools=[{"type": "web_search_preview"}],
)

print(response.output_text)

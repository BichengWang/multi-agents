import asyncio
from agent.story_agent_simple.manager import SimpleStoryManager

# Entrypoint for the simple story agent workflow.
# Run this as `python -m agents.story_agent_simple.main` and enter a story concept query.
async def main() -> None:
    query = input("Enter a story concept or idea to analyze: ")
    mgr = SimpleStoryManager()
    await mgr.run(query)

if __name__ == "__main__":
    asyncio.run(main()) 
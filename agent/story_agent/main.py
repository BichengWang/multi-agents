import asyncio
from agent.story_agent.manager import StoreAgentManager

# Entrypoint for the store agent workflow.
# Run this as `python -m agents.story_agent.main` and enter a store concept query.
async def main() -> None:
    query = input("Enter a store concept or business idea to analyze: ")
    mgr = StoreAgentManager()
    await mgr.run(query)

if __name__ == "__main__":
    asyncio.run(main()) 
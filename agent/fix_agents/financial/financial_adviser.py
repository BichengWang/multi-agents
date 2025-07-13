from agents import Agent, Runner, RunConfig
import asyncio
from typing import List, Dict


def create_agents() -> Dict[str, Agent]:
    """Create and return a dictionary of all agents for financial analysis."""
    agents = {
        "government": Agent(
            name="Government agent",
            instructions="You are a government agent. You are responsible for analyzing the financial data of the country and make the decision of the spending base on the scenario.",
        ),
        "fed": Agent(
            name="Fed agent",
            instructions="You are the FED agent. You are responsible for analyzing the financial data of the country and make the decision of the interest rate on the scenario.",
        ),
        "scenario": Agent(
            name="Scenario agent",
            instructions="You a scenario generator, you are responsible for generating a scenario based on the financial data of the country.",
        ),
        "investor": Agent(
            name="Investor agent",
            instructions="You are responsible for analyzing the financial data of the country and make the investment suggestion decision base on the scenario",
        ),
        "triage": Agent(
            name="Triage agent",
            instructions="Handoff to the appropriate agent based on the scenario",
            handoffs=["government", "scenario", "investor", "fed"],
        ),
        "investment": Agent(
            name="Investment agent",
            instructions="You are responsible the response to the investment strategy based on the scenario",
            handoffs=["triage"],
        )
    }
    return agents


async def run_analysis(input_text: str, agents: Dict[str, Agent], model: str = "o3-mini") -> Dict:
    """Run financial analysis with multiple agents."""
    investment_agent = agents["investment"]
    result = await Runner.run(
        investment_agent,
        input=input_text,
        run_config=RunConfig(model=model, workflow_name="Financial Analysis"),
    )
    
    output = {
        "chain_of_thought": [],
        "raw_responses": result.raw_responses,
        "final_output": result.final_output
    }
    
    if hasattr(result, "chain_of_thought"):
        for step in result.chain_of_thought:
            agent_name = step.get("agent", "Unknown Agent")
            thought = step.get("thought", "")
            output["chain_of_thought"].append({
                "agent": agent_name,
                "thought": thought
            })
    
    return output


async def main():
    # Create all agents
    agents = create_agents()
    
    # Get user input with example
    print("Enter your financial analysis query (or press Enter to use example):")
    print("Example: Mimic the financial scenario when the US debt going up from 2023 to 2030, and provide the investment strategy based on the scenario")
    user_input = input("> ").strip()
    
    # Use example if no input provided
    input_text = user_input if user_input else "Mimic the financial scenario when the US debt going up from 2023 to 2030, and provide the investment strategy based on the scenario"
    
    # Run analysis
    result = await run_analysis(input_text, agents)
    
    # Print results
    for thought in result["chain_of_thought"]:
        print(f"{thought['agent']} chain of thought:\n{thought['thought']}\n{'-'*40}")
    
    print("Final output:")
    print(result["raw_responses"])
    print(result["final_output"])


if __name__ == "__main__":
    asyncio.run(main())

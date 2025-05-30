from agents import Agent, Runner, RunConfig
import asyncio


def create_agents():
    government_agent = Agent(
        name="Government agent",
        instructions="You are a government agent. You are responsible for analyzing the financial data of the country and make the decision of the spending base on the scenario.",
    )

    fed_agent = Agent(
        name="Fed agent",
        instructions="You are the FED agent. You are responsible for analyzing the financial data of the country and make the decision of the interest rate on the scenario.",
    )

    scenario_agent = Agent(
        name="Scenario agent",
        instructions="You a scenario generator, you are responsible for generating a scenario based on the financial data of the country.",
    )

    investor_agent = Agent(
        name="Investor agent",
        instructions="You are responsible for analyzing the financial data of the country and make the investment suggestion decision base on the scenario",
    )

    triage_agent = Agent(
        name="Triage agent",
        instructions="Handoff to the appropriate agent based on the scenario",
        handoffs=[government_agent, scenario_agent, investor_agent, fed_agent],
    )

    investment_agent = Agent(
        name="Investment agent",
        instructions="You are responsible the response to the investment strategy based on the scenario",
        handoffs=[triage_agent],
    )

    return investment_agent


async def main():
    investment_agent = create_agents()
    result = await Runner.run(
        investment_agent,
        input="Mimic the financial scenario when the US debt going up from 2023 to 2030, and provide the investment strategy based on the scenario",
        run_config=RunConfig(model="o3-mini", workflow_name="Financial Analysis"),
    )
    if hasattr(result, "chain_of_thought"):
        for step in result.chain_of_thought:
            agent_name = step.get("agent", "Unknown Agent")
            thought = step.get("thought", "")
            print(f"{agent_name} chain of thought:\n{thought}\n{'-'*40}")
    print("Final output:")
    print(result.raw_responses)
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())

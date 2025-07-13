"""
This is the main file for the store agents:
The agents graph

store generator -> store explainer -> store evaluator -> store manager

"""
# Import from the OpenAI Agents SDK source directly to avoid local agents module shadowing
import sys
from agents import Agent, Runner, RunConfig
import asyncio
from typing import Dict


def create_store_agents() -> Dict[str, Agent]:
    """Create and return a dictionary of all store agents."""
    agents = {
        "store_generator": Agent(
            name="Store Generator",
            instructions="""You are a creative store generator agent. Your role is to:
            1. Generate innovative store concepts and business ideas
            2. Consider market trends, customer needs, and business viability
            3. Provide detailed store descriptions including:
               - Store name and concept
               - Target market and customer demographics
               - Product/service offerings
               - Location and store layout suggestions
               - Unique selling propositions
            4. Focus on creating stores that are both profitable and appealing to customers
            5. Consider different store types: retail, food service, specialty shops, etc.
            
            Always provide comprehensive, well-thought-out store concepts with clear business rationale.""",
        ),
        
        "store_explainer": Agent(
            name="Store Explainer",
            instructions="""You are a store explainer agent. Your role is to:
            1. Take store concepts from the generator and provide detailed explanations
            2. Break down the business model and operational aspects
            3. Explain the customer journey and experience
            4. Detail the marketing strategy and competitive advantages
            5. Provide insights on:
               - Pricing strategy
               - Inventory management
               - Staff requirements
               - Technology needs
               - Risk factors and mitigation strategies
            6. Make complex business concepts accessible and understandable
            7. Highlight the unique value proposition of each store concept
            
            Always provide clear, educational explanations that help stakeholders understand the business model.""",
        ),
        
        "store_evaluator": Agent(
            name="Store Evaluator",
            instructions="""You are a store evaluator agent. Your role is to:
            1. Critically assess store concepts and explanations
            2. Evaluate business viability and market potential
            3. Analyze financial feasibility and profitability
            4. Assess risks and challenges
            5. Provide scoring and ratings on:
               - Market opportunity (1-10)
               - Financial viability (1-10)
               - Operational complexity (1-10)
               - Competitive advantage (1-10)
               - Overall recommendation (1-10)
            6. Identify potential issues and improvement opportunities
            7. Compare against industry benchmarks and best practices
            8. Provide constructive feedback for optimization
            
            Always provide objective, data-driven evaluations with specific recommendations.""",
        ),
        
        "store_manager": Agent(
            name="Store Manager",
            instructions="""You are a store manager agent. Your role is to:
            1. Take evaluated store concepts and create actionable implementation plans
            2. Develop detailed operational strategies and timelines
            3. Create resource allocation and budget plans
            4. Design staffing and training programs
            5. Develop marketing and customer acquisition strategies
            6. Plan inventory and supply chain management
            7. Create risk management and contingency plans
            8. Provide implementation roadmaps with milestones
            9. Consider scaling and growth strategies
            10. Address regulatory and compliance requirements
            
            Always provide practical, actionable management plans that can be executed successfully.""",
        ),
        
        "store_coordinator": Agent(
            name="Store Coordinator",
            instructions="""You are a store coordinator agent. Your role is to:
            1. Orchestrate the entire store development process
            2. Coordinate between generator, explainer, evaluator, and manager agents
            3. Ensure smooth handoffs between different stages
            4. Maintain consistency and quality throughout the process
            5. Synthesize all outputs into a comprehensive final report
            6. Identify any gaps or inconsistencies in the workflow
            7. Provide executive summary and key recommendations
            8. Ensure all deliverables meet quality standards
            
            Always provide a cohesive, well-organized final output that combines all agent insights.""",
            handoffs=["store_generator", "store_explainer", "store_evaluator", "store_manager"],
        )
    }
    return agents


async def run_store_analysis(input_text: str, agents: Dict[str, Agent], model: str = "gpt-4o-mini") -> Dict:
    """Run store analysis with multiple agents."""
    coordinator_agent = agents["store_coordinator"]
    result = await Runner.run(
        coordinator_agent,
        input=input_text,
        run_config=RunConfig(model=model, workflow_name="Store Analysis"),
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
    """Main function to run the store analysis workflow."""
    # Create all agents
    agents = create_store_agents()
    
    # Get user input with example
    print("Enter your store analysis query (or press Enter to use example):")
    print("Example: Create a sustainable coffee shop concept for a university campus")
    user_input = input("> ").strip()
    
    # Use example if no input provided
    input_text = user_input if user_input else "Create a sustainable coffee shop concept for a university campus"
    
    # Run analysis
    result = await run_store_analysis(input_text, agents)
    
    # Print results
    print("\n" + "="*60)
    print("STORE ANALYSIS WORKFLOW RESULTS")
    print("="*60)
    
    for thought in result["chain_of_thought"]:
        print(f"\n{thought['agent']} Analysis:")
        print("-" * 40)
        print(thought['thought'])
        print("-" * 40)
    
    print("\nFINAL OUTPUT:")
    print("="*60)
    print(result["final_output"])


if __name__ == "__main__":
    asyncio.run(main())
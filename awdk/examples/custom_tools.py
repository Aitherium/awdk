"""Agent with custom @tool functions."""

import asyncio
import random
from adk import AitherAgent, tool

# Define tools using the @tool decorator
@tool
def roll_dice(sides: int = 6) -> str:
    """Roll a dice with the specified number of sides."""
    result = random.randint(1, sides)
    return f"Rolled a {result} on a d{sides}"

@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression safely."""
    allowed = set("0123456789+-*/().% ")
    if not all(c in allowed for c in expression):
        return "Error: only basic math operators allowed"
    try:
        result = eval(expression)  # Safe: only math chars allowed
        return str(result)
    except Exception as e:
        return f"Error: {e}"

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city (mock)."""
    temps = {"new york": 72, "london": 58, "tokyo": 65, "sydney": 78}
    temp = temps.get(city.lower(), random.randint(50, 90))
    return f"Weather in {city}: {temp}F, partly cloudy"


async def main():
    # Create agent — tools registered via @tool decorator are auto-discovered
    from adk.tools import get_global_registry
    agent = AitherAgent("atlas", tools=[get_global_registry()])

    # The agent will use tools when appropriate
    response = await agent.chat("Roll two d20s and tell me the total")
    print(response.content)

    response = await agent.chat("What's the weather in Tokyo?")
    print(response.content)
    print(f"Tools used: {response.tool_calls_made}")

if __name__ == "__main__":
    asyncio.run(main())

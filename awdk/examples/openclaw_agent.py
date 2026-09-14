"""Real-world example: a web research agent (OpenClaw-style)."""

import asyncio
import json
from adk import AitherAgent, tool, ToolRegistry

# Build a custom tool registry for this agent
research_tools = ToolRegistry()

@research_tools.register
def search_web(query: str) -> str:
    """Search the web for information. Returns top results."""
    # In production, integrate with a real search API
    return json.dumps({
        "results": [
            {"title": f"Result for: {query}", "snippet": "This is a mock search result. Replace with real API."},
            {"title": f"More about {query}", "snippet": "Another relevant result from web search."},
        ]
    })

@research_tools.register
def read_webpage(url: str) -> str:
    """Read and extract text content from a webpage."""
    # In production, use httpx + BeautifulSoup or similar
    return f"[Mock] Content from {url}: This would be the extracted text from the webpage."

@research_tools.register
def save_note(topic: str, content: str) -> str:
    """Save a research note for later reference."""
    return f"Saved note on '{topic}': {content[:100]}..."

@research_tools.register
def summarize_findings(notes: str) -> str:
    """Compile research notes into a structured summary."""
    return f"Summary compiled from notes: {notes[:200]}..."


async def main():
    # Create a research agent with Lyra's identity (the researcher)
    agent = AitherAgent(
        "openclaw",
        identity="lyra",  # Uses Lyra's research-oriented personality
        tools=[research_tools],
        system_prompt=(
            "You are OpenClaw, a web research agent. "
            "Use your tools to search the web, read pages, and compile research. "
            "Always save important findings as notes. "
            "Provide thorough, well-sourced answers."
        ),
    )

    # Run a research task
    response = await agent.run(
        "Research the current state of AI agent frameworks in 2026. "
        "Compare at least 3 frameworks and summarize their strengths."
    )
    print(response.content)
    print(f"\nTools used: {response.tool_calls_made}")
    print(f"Tokens: {response.tokens_used}")

if __name__ == "__main__":
    asyncio.run(main())

"""Cloud-powered agent using Aitherium's infrastructure — 3 lines to get started.

No local GPU needed. Uses mcp.aitherium.com for inference and 100+ MCP tools.

Setup:
    pip install awdk
    export AITHER_API_KEY=aither_sk_live_...   # Get one at aitherium.com
    python cloud_agent.py
"""

import asyncio
from adk import AitherAgent

async def main():
    # That's it. AITHER_API_KEY in env → auto-connects to Aitherium cloud inference.
    agent = AitherAgent("my-agent")

    # Chat — routes to the right model based on task complexity
    response = await agent.chat("Explain quantum computing in simple terms")
    print(f"[{response.model}] {response.content}")

    # Tools are auto-discovered from the MCP gateway
    # Image generation, code analysis, web search, memory — all available
    response = await agent.chat("Search the web for latest AI news and summarize")
    print(f"\nTools used: {response.tool_calls_made}")
    print(f"Response: {response.content}")


if __name__ == "__main__":
    asyncio.run(main())

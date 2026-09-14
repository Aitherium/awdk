"""Minimal AitherADK agent — 20 lines, talks to Ollama."""

import asyncio
from adk import AitherAgent

async def main():
    # Create an agent with the "aither" identity (defaults to Ollama on localhost)
    agent = AitherAgent("aither")

    # Chat
    response = await agent.chat("Hello! What can you help me with?")
    print(f"[{response.model}] {response.content}")

    # Remember something
    await agent.remember("user_preference", "prefers concise answers")

    # Follow-up (uses conversation history automatically)
    response = await agent.chat("Tell me about yourself in one sentence.")
    print(f"[{response.model}] {response.content}")

if __name__ == "__main__":
    asyncio.run(main())

"""Same agent, but using OpenAI (or any OpenAI-compatible API)."""

import asyncio
import os
from adk import AitherAgent
from adk.llm import LLMRouter

async def main():
    # Option 1: OpenAI
    router = LLMRouter(
        provider="openai",
        api_key=os.getenv("OPENAI_API_KEY", "sk-your-key-here"),
        model="gpt-4o-mini",
    )

    agent = AitherAgent("atlas", llm=router)
    response = await agent.chat("Explain quantum computing in one paragraph.")
    print(f"[OpenAI] {response.content}\n")

    # Option 2: Local vLLM / LM Studio / llama.cpp (same OpenAI format)
    local_router = LLMRouter(
        provider="openai",
        base_url="http://localhost:8000/v1",  # vLLM default
        model="meta-llama/Llama-3.2-3B-Instruct",
    )

    local_agent = AitherAgent("demiurge", llm=local_router)
    response = await local_agent.chat("Write a Python fibonacci function.")
    print(f"[Local vLLM] {response.content}\n")

    # Option 3: Anthropic
    anthropic_router = LLMRouter(
        provider="anthropic",
        api_key=os.getenv("ANTHROPIC_API_KEY", "sk-ant-your-key"),
        model="claude-sonnet-4-6",
    )

    claude_agent = AitherAgent("lyra", llm=anthropic_router)
    response = await claude_agent.chat("What are the latest breakthroughs in AI?")
    print(f"[Anthropic] {response.content}")

if __name__ == "__main__":
    asyncio.run(main())

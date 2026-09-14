"""Two agents collaborating on a task."""

import asyncio
from adk import AitherAgent

async def main():
    # Create two specialized agents
    researcher = AitherAgent("lyra")   # Research specialist
    coder = AitherAgent("demiurge")    # Code specialist

    # Step 1: Researcher analyzes the problem
    research = await researcher.chat(
        "Analyze the best approach for building a REST API in Python. "
        "Consider FastAPI vs Flask vs Django REST. Give a recommendation."
    )
    print(f"[Lyra - Research]\n{research.content}\n")
    print("=" * 60)

    # Step 2: Coder implements based on research
    implementation = await coder.chat(
        f"Based on this research, implement a minimal REST API:\n\n"
        f"{research.content}\n\n"
        f"Write a complete, runnable example."
    )
    print(f"[Demiurge - Implementation]\n{implementation.content}\n")
    print("=" * 60)

    # Step 3: Researcher reviews
    review = await researcher.chat(
        f"Review this implementation for completeness and best practices:\n\n"
        f"{implementation.content}"
    )
    print(f"[Lyra - Review]\n{review.content}")

    # Show total token usage
    total = research.tokens_used + implementation.tokens_used + review.tokens_used
    print(f"\nTotal tokens used: {total}")

if __name__ == "__main__":
    asyncio.run(main())

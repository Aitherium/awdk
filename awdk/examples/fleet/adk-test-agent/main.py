"""ADK Fleet Test Agent — Validates the ADK ↔ Agent Fleet integration.

This is a minimal agent that registers itself in the fleet and serves as
a debugging/development aid for ADK developers. Run it to verify:

1. ADK agent starts and responds to /health
2. Fleet panel discovers it via the constellation API
3. Chat works through the OpenAI-compatible endpoint
4. Federation connects to a running AitherOS instance (optional)

Usage:
    cd awdk/examples/fleet/adk-test-agent
    python main.py

    # Verify:
    curl http://localhost:8800/health
    curl -X POST http://localhost:8800/chat -d '{"message": "hello"}'
"""

import asyncio
import sys
from pathlib import Path

# Add ADK to path if running from source
adk_root = Path(__file__).resolve().parent.parent.parent.parent
if (adk_root / "adk").exists():
    sys.path.insert(0, str(adk_root))

from adk import AitherAgent, tool, ToolRegistry, Config
from adk.server import create_app

# ── Custom Tools ──
tools = ToolRegistry()


@tools.register
def hello(name: str = "developer") -> str:
    """Say hello — verify the ADK tool pipeline works."""
    return f"👋 Hello, {name}! I'm the ADK Test Agent. Tools are working correctly."


@tools.register
def echo(text: str) -> str:
    """Echo back text — useful for debugging tool call serialization."""
    return f"Echo: {text}"


@tools.register
def adk_info() -> str:
    """Return ADK version and configuration info."""
    from adk import __version__
    config = Config.from_env()
    return (
        f"ADK Version: {__version__}\n"
        f"LLM Backend: {config.llm_backend}\n"
        f"Model: {config.model or '(auto-detect)'}\n"
        f"Ollama Host: {config.ollama_host}\n"
        f"Server Port: {config.server_port}\n"
        f"Data Dir: {config.data_dir}\n"
    )


@tools.register
async def test_federation(host: str = "http://localhost") -> str:
    """Test federation connectivity to a running AitherOS instance."""
    from adk.federation import FederationClient
    fed = FederationClient(host)
    try:
        # Try to hit Pulse (always running)
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{host}:8081/health")
            if resp.status_code == 200:
                return f"✅ AitherOS reachable at {host} — Pulse is healthy"
            return f"⚠️ AitherOS at {host} returned status {resp.status_code}"
    except Exception as e:
        return f"❌ Cannot reach AitherOS at {host}: {e}"


# ── Agent ──
agent = AitherAgent(
    name="adk-test-agent",
    tools=tools,
    system_prompt=(
        "You are the ADK Test Agent — a debugging companion for developers "
        "building new agents with the AitherOS Agent Development Kit. "
        "You help verify that tools work, federation connects, and the "
        "agent lifecycle is functioning properly. Be concise and helpful."
    ),
    config=Config(
        server_port=8800,
        llm_backend="auto",
    ),
)

# ── Server ──
app = create_app(agent=agent, identity="adk-test-agent")

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  ADK Test Agent — Fleet Integration Debugger")
    print("=" * 60)
    print(f"  Health:  GET  http://localhost:8800/health")
    print(f"  Chat:    POST http://localhost:8800/chat")
    print(f"  OpenAI:  POST http://localhost:8800/v1/chat/completions")
    print(f"  Docs:    GET  http://localhost:8800/docs")
    print(f"  Models:  GET  http://localhost:8800/v1/models")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info")

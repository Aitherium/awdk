#!/usr/bin/env python3
"""Federation Demo — External agent connecting to a running AitherOS instance.

This demonstrates the full federation stack:
1. Self-setup: detect hardware, ensure Ollama, pull models
2. Register with AitherIdentity (authentication)
3. Join AitherMesh (node network)
4. Subscribe to Flux events (event routing)
5. Use MCP tools (code search, memory, agent dispatch)
6. Chat via Genesis (full pipeline)
7. Operate in isolated public tenant

Prerequisites:
    - AitherOS instance running (Genesis:8001, Identity:8112, Mesh:8125, Node:8090)
    - pip install awdk

Usage:
    python federation_demo.py
    python federation_demo.py --host http://192.168.1.100
    python federation_demo.py --mesh-key mk-xxxxx  # pre-generated mesh key
    python federation_demo.py --skip-setup          # skip hardware setup
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Add parent to path if running from examples/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk import AitherAgent
from adk.federation import FederationClient
from adk.setup import AgentSetup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("federation_demo")


async def phase_1_self_setup(skip: bool = False):
    """Phase 1: Detect hardware, ensure inference backend, pull models."""
    print("\n" + "=" * 60)
    print("  PHASE 1: Self-Setup")
    print("=" * 60)

    if skip:
        print("  Skipped (--skip-setup)")
        return

    setup = AgentSetup()
    info = await setup.detect_hardware()

    print(f"  OS:      {info.os_name} ({info.arch})")
    print(f"  RAM:     {info.ram_gb} GB")
    print(f"  GPU:     {info.gpu.vendor} — {info.gpu.name or 'none'}")
    if info.gpu.vram_mb:
        print(f"  VRAM:    {info.gpu.vram_mb} MB")
    if info.gpu.cuda_version:
        print(f"  CUDA:    {info.gpu.cuda_version}")
    print(f"  Profile: {info.profile}")
    print(f"  Ollama:  {'running' if info.ollama_running else 'installed' if info.ollama_installed else 'not found'}")
    print(f"  Docker:  {'available' if info.docker_installed else 'not found'}")

    if info.ollama_models:
        print(f"  Models:  {', '.join(info.ollama_models[:5])}")

    if not info.ollama_running:
        print("\n  Ensuring Ollama is ready...")
        ok = await setup.ensure_ollama()
        if ok:
            print("  Ollama is running!")
        else:
            print("  WARNING: Ollama not available — chat will need cloud LLM")

    # Pull a small model if nothing is available
    if not info.ollama_models:
        print("\n  Pulling a starter model...")
        pulled = await setup.pull_models(["llama3.2:1b"])
        if pulled:
            print(f"  Pulled: {', '.join(pulled)}")


async def phase_2_register(fed: FederationClient, args):
    """Phase 2: Register with AitherIdentity."""
    print("\n" + "=" * 60)
    print("  PHASE 2: AitherIdentity Registration")
    print("=" * 60)

    if args.mesh_key:
        # Mesh key enrollment (no prior auth)
        print(f"  Enrolling with mesh key: {args.mesh_key[:8]}...")
        creds = await fed.enroll_with_mesh_key(
            mesh_key=args.mesh_key,
            capabilities={
                "skills": ["text_generation", "code_analysis"],
                "backends": ["ollama"],
            },
        )
    else:
        # Standard registration
        username = args.username or f"demo-agent-{fed._node_id[:6]}"
        print(f"  Registering as: {username}")
        creds = await fed.register(
            username=username,
            password=args.password or "demo-password-2026",
            api_key=args.api_key or "",
        )

    if creds.token or creds.api_key:
        print(f"  Authenticated! Node ID: {fed._node_id}")
        if creds.wireguard_ip:
            print(f"  WireGuard IP: {creds.wireguard_ip}")
        return True
    else:
        print("  WARNING: Authentication failed — continuing with limited access")
        return False


async def phase_3_join_mesh(fed: FederationClient):
    """Phase 3: Join AitherMesh network."""
    print("\n" + "=" * 60)
    print("  PHASE 3: AitherMesh — Join Network")
    print("=" * 60)

    joined = await fed.join_mesh(
        capabilities=["text_generation", "code_analysis", "tool_use"],
        role="client",
    )

    if joined:
        print("  Joined mesh network!")
        nodes = await fed.list_nodes()
        if nodes:
            print(f"  Discovered {len(nodes)} node(s):")
            for n in nodes[:5]:
                print(f"    - {n.node_id} ({n.role}) [{n.status}]")
    else:
        print("  WARNING: Could not join mesh — services may be down")

    return joined


async def phase_4_flux_events(fed: FederationClient):
    """Phase 4: Subscribe to Flux events."""
    print("\n" + "=" * 60)
    print("  PHASE 4: AitherFlux — Event Routing")
    print("=" * 60)

    events_received = []

    def on_pain(event):
        events_received.append(event)
        etype = event.get("type", "unknown")
        logger.info(f"Flux event: {etype}")

    fed.on_event("PAIN_SIGNAL", on_pain)
    fed.on_event("SERVICE_HEALTH", on_pain)
    fed.on_event("AGENT_REGISTERED", on_pain)

    # Emit our own event announcing we've arrived
    emitted = await fed.emit_event(
        "EXTERNAL_AGENT_CONNECTED",
        {"node_id": fed._node_id, "capabilities": ["text_generation"]},
    )
    print(f"  Emitted EXTERNAL_AGENT_CONNECTED: {'ok' if emitted else 'failed'}")

    # Check system status
    status = await fed.get_system_status()
    print(f"  System status: {status.get('status', 'unknown')}")

    return emitted


async def phase_5_mcp_tools(fed: FederationClient):
    """Phase 5: Use MCP tools from awnode."""
    print("\n" + "=" * 60)
    print("  PHASE 5: MCP Tools — awnode")
    print("=" * 60)

    tools = await fed.list_mcp_tools()
    if tools:
        print(f"  Available tools: {len(tools)}")
        for t in tools[:10]:
            name = t.get("name", "?")
            desc = t.get("description", "")[:50]
            print(f"    - {name}: {desc}")

        # Try a few tools
        print("\n  Testing tools...")

        # 1. Get system status
        result = await fed.call_mcp_tool("get_system_status")
        if result:
            print(f"  get_system_status: {result[:100]}...")

        # 2. Code search
        result = await fed.call_mcp_tool("explore_code", {"query": "AitherAgent"})
        if result:
            print(f"  explore_code: {result[:100]}...")

        # 3. List identities
        result = await fed.call_mcp_tool("list_souls")
        if result:
            print(f"  list_souls: {result[:100]}...")

        return True
    else:
        print("  WARNING: No MCP tools available (awnode may be down)")
        return False


async def phase_6_chat(fed: FederationClient):
    """Phase 6: Chat via Genesis (full pipeline)."""
    print("\n" + "=" * 60)
    print("  PHASE 6: Genesis Chat — Full Pipeline")
    print("=" * 60)

    messages = [
        ("What services are running right now?", "atlas", 3),
        ("Summarize the AitherOS architecture in 2 sentences.", "aither", 5),
    ]

    for msg, agent, effort in messages:
        print(f"\n  [{agent}] Q: {msg}")
        result = await fed.chat(msg, agent=agent, effort=effort)
        source = result.get("source", "none")
        response = result.get("response", "")
        if response:
            # Truncate long responses
            display = response[:200] + "..." if len(response) > 200 else response
            print(f"  [{agent}] A ({source}): {display}")
        else:
            print(f"  [{agent}] No response (source={source})")

    return True


async def phase_7_agent_workflow(fed: FederationClient):
    """Phase 7: Full agent workflow — create a local agent that uses federation tools."""
    print("\n" + "=" * 60)
    print("  PHASE 7: Full Agent Workflow")
    print("=" * 60)

    # Create a local ADK agent
    agent = AitherAgent("atlas", system_prompt=(
        "You are Atlas, a research and analysis agent. "
        "You have access to AitherOS federation tools. "
        "Answer questions about the system architecture and status."
    ))

    # Register federation tools as agent tools
    @agent.tool
    async def aitheros_status() -> str:
        """Get the current AitherOS system status."""
        status = await fed.get_system_status()
        return json.dumps(status, indent=2)

    @agent.tool
    async def search_code(query: str) -> str:
        """Search the AitherOS codebase."""
        return await fed.call_mcp_tool("explore_code", {"query": query})

    @agent.tool
    async def ask_genesis(question: str) -> str:
        """Ask the AitherOS Genesis orchestrator a question."""
        result = await fed.chat(question, agent="aither", effort=5)
        return result.get("response", "No response")

    @agent.tool
    async def list_mesh_nodes() -> str:
        """List nodes in the AitherMesh network."""
        nodes = await fed.list_nodes()
        return json.dumps([{"id": n.node_id, "role": n.role, "status": n.status} for n in nodes])

    print(f"  Created local agent 'atlas' with {len(agent._tools.list_tools())} federation tools")

    # Run the agent
    try:
        response = await agent.chat(
            "What is the current system status? Use your tools to check.",
        )
        print(f"\n  Agent response: {response.content[:300]}")
        if response.tool_calls_made:
            print(f"  Tools used: {', '.join(response.tool_calls_made)}")
    except Exception as e:
        print(f"  Agent chat error (expected if no LLM available): {e}")

    return True


import json


async def main():
    parser = argparse.ArgumentParser(description="AitherOS Federation Demo")
    parser.add_argument("--host", default="http://localhost", help="AitherOS host")
    parser.add_argument("--mesh-key", default="", help="Pre-generated mesh key")
    parser.add_argument("--api-key", default="", help="Existing API key")
    parser.add_argument("--username", default="", help="Registration username")
    parser.add_argument("--password", default="", help="Registration password")
    parser.add_argument("--skip-setup", action="store_true", help="Skip hardware setup")
    parser.add_argument("--phases", default="1,2,3,4,5,6,7", help="Phases to run (comma-separated)")
    args = parser.parse_args()

    phases = set(int(p) for p in args.phases.split(","))

    print("\n" + "=" * 60)
    print("  AitherOS Federation Demo")
    print(f"  Connecting to: {args.host}")
    print("=" * 60)

    results = {}

    # Phase 1: Self-Setup
    if 1 in phases:
        await phase_1_self_setup(skip=args.skip_setup)
        results["self_setup"] = True

    async with FederationClient(host=args.host) as fed:
        # Phase 2: Register
        if 2 in phases:
            results["registered"] = await phase_2_register(fed, args)

        # Phase 3: Join Mesh
        if 3 in phases:
            results["mesh_joined"] = await phase_3_join_mesh(fed)

        # Phase 4: Flux Events
        if 4 in phases:
            results["flux_connected"] = await phase_4_flux_events(fed)

        # Phase 5: MCP Tools
        if 5 in phases:
            results["mcp_tools"] = await phase_5_mcp_tools(fed)

        # Phase 6: Chat
        if 6 in phases:
            results["chat"] = await phase_6_chat(fed)

        # Phase 7: Agent Workflow
        if 7 in phases:
            results["agent_workflow"] = await phase_7_agent_workflow(fed)

    # Summary
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    for phase, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {phase:20s}: {status}")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} phases completed successfully")


if __name__ == "__main__":
    asyncio.run(main())

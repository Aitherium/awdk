#!/usr/bin/env python3
"""
FULL LIFECYCLE TEST — Proves the entire ADK → Gateway → AitherOS pipeline works.

This script simulates what an EXTERNAL agent does:

1. Connect to gateway (gateway.aitherium.com / localhost:8185)
2. Register and get API key
3. Verify authentication
4. Check system status
5. Join the mesh
6. List mesh nodes
7. Emit a Flux event
8. List MCP tools
9. Chat via Genesis
10. Send telemetry
11. Submit a bug report

Run with: python examples/full_lifecycle_test.py [--gateway URL]
"""

import asyncio
import json
import sys
import time

import httpx

# ── Configuration ──────────────────────────────────────────────────────────

GATEWAY = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8185"

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def phase(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    icon = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if detail:
        print(f"         {CYAN}{detail}{RESET}")


async def main():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  AitherOS Full Lifecycle Test{RESET}")
    print(f"  Gateway: {CYAN}{GATEWAY}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    api_key = ""

    # ── Phase 1: Health Check ──────────────────────────────────────────
    print(f"{BOLD}Phase 1: Gateway Health{RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{GATEWAY}/health")
            data = r.json()
            phase("Gateway reachable", r.status_code == 200,
                  f"service={data.get('service')} port={data.get('port')}")
    except Exception as e:
        phase("Gateway reachable", False, str(e))
        print(f"\n{RED}Gateway not running. Start it first:{RESET}")
        print(f"  cd AitherOS && python -m services.mesh.AitherExternalGateway")
        return

    # ── Phase 2: System Status ─────────────────────────────────────────
    print(f"\n{BOLD}Phase 2: System Status (no auth required){RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{GATEWAY}/v1/status")
            data = r.json()
            services = data.get("services", {})
            up = [k for k, v in services.items() if v]
            down = [k for k, v in services.items() if not v]
            phase("Status endpoint", r.status_code == 200,
                  f"up={up} down={down}")
    except Exception as e:
        phase("Status endpoint", False, str(e))

    # ── Phase 3: Register Agent ────────────────────────────────────────
    print(f"\n{BOLD}Phase 3: Agent Registration{RESET}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{GATEWAY}/v1/auth/register", json={
                "display_name": "lifecycle-test-agent",
                "description": "Full lifecycle test agent",
                "clearance": "CONTRIBUTOR",
                "capabilities": ["text_generation", "code_analysis"],
            })
            data = r.json()
            api_key = data.get("api_key", "")
            agent_id = data.get("agent_id", "")
            phase("Agent registered", bool(api_key),
                  f"agent_id={agent_id} key={'ak_...' + api_key[-6:] if api_key else 'NONE'}")
    except Exception as e:
        phase("Agent registered", False, str(e))

    if not api_key:
        print(f"\n{YELLOW}No API key — remaining tests will use unauthenticated access{RESET}")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # ── Phase 4: Verify Auth ───────────────────────────────────────────
    print(f"\n{BOLD}Phase 4: Verify Authentication{RESET}")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{GATEWAY}/v1/auth/me", headers=headers)
                data = r.json()
                phase("Auth verified", r.status_code == 200,
                      f"agent_id={data.get('agent_id')} clearance={data.get('clearance')}")
        except Exception as e:
            phase("Auth verified", False, str(e))
    else:
        phase("Auth verified", False, "No API key to verify")

    # ── Phase 5: Join Mesh ─────────────────────────────────────────────
    print(f"\n{BOLD}Phase 5: Mesh Join{RESET}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{GATEWAY}/v1/mesh/join", headers=headers, json={
                "node_name": "lifecycle-test-node",
                "capabilities": ["text_generation", "code_analysis"],
            })
            data = r.json()
            phase("Joined mesh", r.status_code == 200,
                  f"response={json.dumps(data)[:120]}")
    except Exception as e:
        phase("Joined mesh", False, str(e))

    # ── Phase 6: List Mesh Nodes ───────────────────────────────────────
    print(f"\n{BOLD}Phase 6: Mesh Discovery{RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{GATEWAY}/v1/mesh/nodes", headers=headers)
            data = r.json()
            nodes = data.get("nodes", [])
            phase("List nodes", r.status_code == 200,
                  f"found {len(nodes)} nodes")
    except Exception as e:
        phase("List nodes", False, str(e))

    # ── Phase 7: Emit Flux Event ───────────────────────────────────────
    print(f"\n{BOLD}Phase 7: Flux Event{RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{GATEWAY}/v1/events/emit", headers=headers, json={
                "event_type": "EXTERNAL_AGENT_CONNECTED",
                "data": {"agent": "lifecycle-test", "timestamp": time.time()},
                "source": "lifecycle-test",
            })
            phase("Event emitted", r.status_code == 200,
                  f"status={r.status_code}")
    except Exception as e:
        phase("Event emitted", False, str(e))

    # ── Phase 8: MCP Tools ─────────────────────────────────────────────
    print(f"\n{BOLD}Phase 8: MCP Tools{RESET}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{GATEWAY}/v1/mcp/tools", headers=headers)
            if r.status_code == 200:
                data = r.json()
                tools = data.get("tools", [])
                names = [t.get("name", "") for t in tools[:5]]
                phase("MCP tools listed", len(tools) > 0,
                      f"{len(tools)} tools: {names}...")
            else:
                phase("MCP tools listed", False, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        phase("MCP tools listed", False, str(e))

    # ── Phase 9: Chat via Genesis ──────────────────────────────────────
    print(f"\n{BOLD}Phase 9: Chat (Genesis Pipeline){RESET}")
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(f"{GATEWAY}/v1/chat", headers=headers, json={
                "message": "What is AitherOS? Give a one-sentence answer.",
                "agent": "aither",
            })
            data = r.json()
            response_text = data.get("response", data.get("content", ""))
            phase("Chat response", bool(response_text),
                  f"{response_text[:120]}..." if len(response_text) > 120 else response_text)
    except Exception as e:
        phase("Chat response", False, str(e))

    # ── Phase 10: Telemetry ────────────────────────────────────────────
    print(f"\n{BOLD}Phase 10: Telemetry (opt-in){RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{GATEWAY}/v1/telemetry", headers=headers, json={
                "agent_name": "lifecycle-test",
                "backend_type": "ollama",
                "model_family": "llama3",
                "tool_count": 5,
                "uptime_seconds": 42.0,
                "os_type": "windows",
                "adk_version": "0.1.0",
                "event": "heartbeat",
            })
            phase("Telemetry sent", r.status_code == 200)
    except Exception as e:
        phase("Telemetry sent", False, str(e))

    # ── Phase 11: Bug Report ───────────────────────────────────────────
    print(f"\n{BOLD}Phase 11: Bug Report{RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{GATEWAY}/v1/bugs", headers=headers, json={
                "description": "Test bug report from lifecycle test",
                "os_info": "Windows 11",
                "python_version": "3.12",
                "adk_version": "0.1.0",
                "backend_type": "ollama",
            })
            data = r.json()
            bug_id = data.get("bug_id", "")
            phase("Bug submitted", bool(bug_id),
                  f"bug_id={bug_id}")

            # Verify bug can be retrieved
            if bug_id:
                r2 = await c.get(f"{GATEWAY}/v1/bugs/{bug_id}")
                phase("Bug retrievable", r2.status_code == 200)
    except Exception as e:
        phase("Bug submitted", False, str(e))

    # ── Phase 12: Security Status ────────────────────────────────────
    print(f"\n{BOLD}Phase 12: Security Layers{RESET}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{GATEWAY}/v1/security/status", headers=headers)
            if r.status_code == 200:
                data = r.json()
                layers = data.get("security_layers", {})
                active = [k for k, v in layers.items() if v == "active"]
                degraded = [k for k, v in layers.items() if v == "degraded"]
                phase("Security layers", len(active) >= 4,
                      f"active={active} degraded={degraded}")
            else:
                phase("Security layers", False, f"HTTP {r.status_code}")
    except Exception as e:
        phase("Security layers", False, str(e))

    # ── Summary ────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'='*60}{RESET}")
    passed = sum(1 for _, p, _ in results if p)
    total = len(results)
    color = GREEN if passed == total else (YELLOW if passed > total // 2 else RED)
    print(f"  {color}{BOLD}{passed}/{total} phases passed{RESET}")

    if passed < total:
        print(f"\n  {YELLOW}Failed phases:{RESET}")
        for name, p, detail in results:
            if not p:
                print(f"    - {name}: {detail}")

    print(f"\n  Gateway URL for Cloudflare Tunnel: {CYAN}{GATEWAY}{RESET}")
    print(f"  Set tunnel service to: {CYAN}http://localhost:8185{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())

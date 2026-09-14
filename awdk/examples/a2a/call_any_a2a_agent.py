"""Call ANY external A2A agent from awdk — vendor-neutral.

This talks to a remote agent by its Agent Card URL using plain A2A JSON-RPC
(`message/send`), so it works against a Google-A2A agent, a LangGraph A2A server,
another awdk agent, or anything that implements the spec — no Aitherium SDK
required on the remote side.

(For agents in YOUR OWN awdk fleet you can use the convenience wrapper
`adk.a2a_client.send_message("agent-name", "...")`, which resolves the name via the
mesh. The function below is the general, by-URL path for external interop.)

Run:
    python call_any_a2a_agent.py https://some-a2a-agent.example.com "summarize A2A in one line"
"""
import asyncio
import sys
import uuid

import httpx


async def call_a2a_agent(base_url: str, text: str) -> dict:
    base_url = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60) as c:
        # 1) Discover the agent (A2A Agent Card). Optional, but it's how you learn
        #    the agent's skills/capabilities before talking to it.
        card = (await c.get(f"{base_url}/.well-known/agent-card.json")).json()
        print(f"→ talking to {card.get('name')!r}: {card.get('description', '')[:80]}")

        # 2) Standard A2A message/send — the interop call.
        rpc = {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            }},
        }
        r = (await c.post(f"{base_url}/a2a", json=rpc)).json()

    if r.get("error"):
        raise RuntimeError(f"A2A error: {r['error']}")
    result = r.get("result", {})
    reply = " ".join(p.get("text", "") for p in result.get("message", {}).get("parts", []))
    return {"reply": reply, "task": result.get("task", {})}


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    text = sys.argv[2] if len(sys.argv) > 2 else "Hello over A2A!"
    out = await call_a2a_agent(url, text)
    print(f"← {out['reply']}")
    print(f"  (task state: {out['task'].get('status', {}).get('state')})")


if __name__ == "__main__":
    asyncio.run(main())

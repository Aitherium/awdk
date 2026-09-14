"""
Cloud GPU Plugin for AitherShell
==================================

Self-service GPU deployment — browse offers, launch instances, manage your
private AI cloud, and check running deployments.

Usage:
    /cloud                          — Show running deployments
    /cloud offers [--gpu <type>]    — Browse live GPU marketplace
    /cloud profiles                 — List deployment profiles
    /cloud launch <model>           — Deploy a model to cloud GPU
    /cloud launch <model> --offer <id> — Deploy to a specific offer
    /cloud status <session_id>      — Deployment phase + progress
    /cloud stop <session_id>        — Tear down a deployment
    /cloud pool                     — Unified compute pool view
    /cloud connect                  — Connection info for deployed models
    /cloud signup                   — Self-service cloud tenant signup
    /cloud key <provider> <key>     — Store BYOK provider API key
    /cloud estimate <model>         — Cost estimate before deploying

Aliases: /gpu, /deploy
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


class CloudPlugin(SlashCommand):
    name: str = "cloud"
    aliases: List[str] = ["cloud-gpu", "cloud-deploy"]
    description: str = "Self-service GPU deployment and cloud management"
    category: str = "ai"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='cloud',
            description='Self-service GPU deployment and cloud management',
            aliases=['cloud-gpu', 'cloud-deploy'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._running(args, ctx)

        sub = args[0].lower()
        dispatch = {
            "offers": self._offers,
            "marketplace": self._offers,
            "browse": self._offers,
            "profiles": self._profiles,
            "launch": self._launch,
            "start": self._launch,
            "status": self._status,
            "stop": self._stop,
            "teardown": self._stop,
            "pool": self._pool,
            "connect": self._connect,
            "signup": self._signup,
            "onboard": self._signup,
            "key": self._provider_key,
            "estimate": self._estimate,
            "running": self._running,
            "list": self._running,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)

        # If arg looks like a session ID, show status
        if len(args[0]) > 8:
            return await self._status(args, ctx)

        return await self._running(args, ctx)

    async def _offers(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        params: Dict[str, Any] = {"limit": 10}
        i = 0
        while i < len(args):
            if args[i] in ("--gpu", "--model") and i + 1 < len(args):
                params["gpu_model"] = args[i + 1]
                i += 2
            elif args[i] == "--max-price" and i + 1 < len(args):
                params["max_price"] = float(args[i + 1])
                i += 2
            elif args[i] == "--vram" and i + 1 < len(args):
                params["min_vram_gb"] = int(args[i + 1])
                i += 2
            else:
                # Treat as GPU model name
                params["gpu_model"] = args[i]
                i += 1

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=30) as c:
            resp = await c.get("/cloud/instances/gpu-offers", params=params)

        if resp.status_code != 200:
            return f"❌ Failed to fetch offers: {resp.status_code}"

        data = resp.json()
        offers = data.get("offers", [])
        if not offers:
            return "No GPU offers found matching your criteria. Try `/cloud offers` without filters."

        lines = [f"🖥️ **{len(offers)} GPU Offers**\n"]
        for o in offers:
            gpu = o.get("gpu_model", "?")
            vram = o.get("vram_gb", "?")
            price = o.get("price_per_hour", "?")
            provider = o.get("provider", "?")
            oid = o.get("offer_id", "?")
            lines.append(f"  `{oid}` — **{gpu}** {vram}GB  ${price}/hr  [{provider}]")

        lines.append(f"\nDeploy: `/cloud launch <model> --offer <offer_id>`")
        return "\n".join(lines)

    async def _profiles(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/cloud/instances/profiles")

        if resp.status_code != 200:
            return f"❌ Failed to fetch profiles: {resp.status_code}"

        data = resp.json()
        profiles = data.get("profiles", [])
        if not profiles:
            return "No deployment profiles configured."

        lines = ["📋 **Cloud Node Profiles**\n"]
        for p in profiles:
            name = p.get("name", "?")
            model = p.get("model", "?")
            vram = p.get("min_vram_gb", "?")
            budget = p.get("budget_per_hour", "?")
            lines.append(f"  **{name}** — `{model}` ({vram}GB VRAM, ${budget}/hr)")
        return "\n".join(lines)

    async def _launch(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/cloud launch <model>` — e.g. `/cloud launch deepseek-ai/DeepSeek-R1-0528-Qwen3-8B`"

        model = args[0]
        body: Dict[str, Any] = {"model": model}

        i = 1
        while i < len(args):
            if args[i] == "--offer" and i + 1 < len(args):
                body["offer_id"] = args[i + 1]
                i += 2
            elif args[i] == "--profile" and i + 1 < len(args):
                body["profile"] = args[i + 1]
                i += 2
            elif args[i] == "--max-price" and i + 1 < len(args):
                body["max_price_per_hour"] = float(args[i + 1])
                i += 2
            elif args[i] == "--name" and i + 1 < len(args):
                body["served_name"] = args[i + 1]
                i += 2
            else:
                i += 1

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=30) as c:
            resp = await c.post("/cloud/instances/launch", json=body)

        if resp.status_code != 200:
            return f"❌ Launch failed: {resp.status_code} — {resp.text[:300]}"

        data = resp.json()
        session = data.get("session_id", "?")
        phase = data.get("phase", "started")
        return f"🚀 **Deployment started**\n  Model: `{model}`\n  Session: `{session}`\n  Phase: {phase}\n\nTrack: `/cloud status {session}`"

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/cloud status <session_id>`"

        session_id = args[0]
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get(f"/cloud/instances/{session_id}")

        if resp.status_code != 200:
            return f"❌ Not found: {resp.status_code}"

        data = resp.json()
        inst = data.get("instance", data)
        phase = inst.get("phase", "?")
        model = inst.get("model", "?")
        gpu = inst.get("gpu_model", "")
        url = inst.get("vllm_url", "")

        lines = [f"📊 **Deployment Status**"]
        lines.append(f"  Session: `{session_id}`")
        lines.append(f"  Model: `{model}`")
        lines.append(f"  Phase: **{phase}**")
        if gpu:
            lines.append(f"  GPU: {gpu}")
        if url:
            lines.append(f"  Inference URL: `{url}`")
        return "\n".join(lines)

    async def _stop(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/cloud stop <session_id>`"

        session_id = args[0]
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=30) as c:
            resp = await c.delete(f"/cloud/instances/{session_id}")

        if resp.status_code != 200:
            return f"❌ Teardown failed: {resp.status_code}"

        return f"🛑 Instance `{session_id}` torn down."

    async def _pool(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/cloud/instances/pool/status")

        if resp.status_code != 200:
            return f"❌ Pool status failed: {resp.status_code}"

        data = resp.json()
        backends = data.get("backends", [])
        lines = ["🌐 **Compute Pool**\n"]
        for b in backends:
            status_icon = "🟢" if b.get("status") == "online" else "🔴"
            lines.append(f"  {status_icon} {b.get('name', '?')} — {b.get('status', '?')}")
        if not backends:
            lines.append("  No backends registered")
        return "\n".join(lines)

    async def _connect(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/cloud/instances/connect-info")

        if resp.status_code != 200:
            return f"❌ Connect info failed: {resp.status_code}"

        data = resp.json()
        lines = ["🔗 **Connection Info**\n"]
        gw = data.get("gateway_inference_url", "")
        if gw:
            lines.append(f"  Gateway: `{gw}`")
        for ep in data.get("direct_endpoints", []):
            lines.append(f"  `{ep.get('served_name', '?')}` → `{ep.get('chat_endpoint', '?')}`")
        models = data.get("deployed_models", [])
        if models:
            lines.append(f"\n  Models: {', '.join(f'`{m}`' for m in models)}")
        return "\n".join(lines)

    async def _signup(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return ("Usage: `/cloud signup <email>` [--plan builder|starter|growth|professional]\n"
                    "Creates your private AI cloud tenant.")

        email = args[0]
        plan = "builder"
        i = 1
        while i < len(args):
            if args[i] == "--plan" and i + 1 < len(args):
                plan = args[i + 1]
                i += 2
            else:
                i += 1

        body = {"email": email, "plan": plan}
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=30) as c:
            resp = await c.post("/cloud/instances/onboard", json=body)

        if resp.status_code != 200:
            return f"❌ Signup failed: {resp.status_code} — {resp.text[:200]}"

        data = resp.json()
        lines = ["✅ **Cloud account created!**"]
        if data.get("tenant_id"):
            lines.append(f"  Tenant ID: `{data['tenant_id']}`")
        if data.get("api_key"):
            lines.append(f"  API Key: `{data['api_key']}`")
            lines.append("  ⚠️ **Save this key — it won't be shown again!**")
        return "\n".join(lines)

    async def _provider_key(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if len(args) < 2:
            return "Usage: `/cloud key <provider> <api_key>` — provider: vastai, runpod, lambda"

        provider = args[0]
        api_key = args[1]

        body = {"provider": provider, "api_key": api_key}
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.put("/cloud/instances/provider-key", json=body)

        if resp.status_code != 200:
            return f"❌ Failed to store key: {resp.status_code}"

        return f"🔑 Provider key stored for **{provider}**"

    async def _estimate(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/cloud estimate <model>`"

        model = args[0]
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/cloud/instances/estimate", params={"model": model})

        if resp.status_code != 200:
            return f"❌ Estimate failed: {resp.status_code}"

        data = resp.json()
        lines = [f"💰 **Cost Estimate** for `{model}`"]
        if data.get("estimated_cost_per_hour"):
            lines.append(f"  ~${data['estimated_cost_per_hour']}/hr")
        if data.get("min_vram_gb"):
            lines.append(f"  Requires ≥{data['min_vram_gb']}GB VRAM")
        if data.get("recommended_profile"):
            lines.append(f"  Profile: `{data['recommended_profile']}`")
        return "\n".join(lines)

    async def _running(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/cloud/instances/running")

        if resp.status_code != 200:
            return f"❌ Failed to list instances: {resp.status_code}"

        data = resp.json()
        instances = data.get("instances", [])
        if not instances:
            return "No running deployments. Use `/cloud launch <model>` to deploy."

        lines = [f"🖥️ **{len(instances)} Active Deployments**\n"]
        for inst in instances:
            name = inst.get("served_name", inst.get("model", "?"))
            phase = inst.get("phase", inst.get("status", "?"))
            gpu = inst.get("gpu_model", "")
            price = inst.get("price_per_hour", "")
            sid = inst.get("session_id", "?")
            icon = "🟢" if phase in ("complete", "running") else "🟡" if phase == "failed" else "🔵"
            price_str = f"  ${price}/hr" if price else ""
            gpu_str = f"  {gpu}" if gpu else ""
            lines.append(f"  {icon} **{name}** [{phase}]{gpu_str}{price_str}")
            lines.append(f"      Session: `{sid}`")

        return "\n".join(lines)

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()

    def get_help(self) -> str:
        return """☁️ **Cloud GPU Deployment**

| Command | Description |
|---------|-------------|
| `/cloud` | List running deployments |
| `/cloud offers` | Browse GPU marketplace |
| `/cloud profiles` | Available deployment profiles |
| `/cloud launch <model>` | Deploy model to cloud GPU |
| `/cloud status <id>` | Check deployment status |
| `/cloud stop <id>` | Tear down deployment |
| `/cloud pool` | Unified compute pool |
| `/cloud connect` | Connection info for models |
| `/cloud signup <email>` | Create cloud tenant |
| `/cloud key <provider> <key>` | Store BYOK API key |
| `/cloud estimate <model>` | Cost estimate |
"""

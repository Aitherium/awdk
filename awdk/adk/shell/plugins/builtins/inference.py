"""
Inference Plugin for AitherShell
==================================

Configure local/custom LLM inference backends. Lets users bring their own
Ollama, vLLM, or cloud API keys and route all AitherOS inference through them.

Usage:
    /inference                              — Show current config
    /inference set --provider ollama --url http://localhost:11434
    /inference set --provider openai --key sk-... --url https://api.openai.com/v1
    /inference set --provider vllm --url http://192.168.1.10:8000
    /inference map reasoning deepseek-r1:14b   — Route reasoning to local model
    /inference map chat llama3.2:8b             — Route chat to local model
    /inference map coding qwen2.5-coder:7b     — Route coding to local model
    /inference test                             — Test connectivity to backend
    /inference clear                            — Remove custom config (use platform default)
    /inference models                           — List models on your backend

Aliases: /inf-config, /backend
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _identity_url() -> str:
    return os.environ.get(
        "AITHER_IDENTITY_URL",
        os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"),
    )


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _require_auth() -> Optional[str]:
    if not AuthStore:
        return "Auth module not available. Run `aither setup` first."
    if not AuthStore.get_active_token():
        return "Not logged in. Run `aither setup` to authenticate."
    return None


class InferencePlugin(SlashCommand):
    name: str = "inference"
    aliases: List[str] = ["inf-config", "backend"]
    description: str = "Configure local/custom LLM inference backend"
    category: str = "ai"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='inference',
            description='Configure local/custom LLM inference backend',
            aliases=['inf-config', 'backend'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        err = _require_auth()
        if err:
            return err

        if not args:
            return await self._show(args, ctx)

        sub = args[0].lower()
        dispatch = {
            "set": self._set,
            "configure": self._set,
            "map": self._map,
            "test": self._test,
            "clear": self._clear,
            "remove": self._clear,
            "reset": self._clear,
            "models": self._models,
            "show": self._show,
            "status": self._show,
            "mode": self._mode,
            "switch": self._mode,
            "stack": self._mode,
            "stacks": self._stacks,
            "help": self._help,
        }
        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)
        return await self._help(args, ctx)

    async def _show(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/auth/me/inference-config")

        if resp.status_code == 404 or (resp.status_code == 200 and not resp.json().get("config")):
            return (
                "**No custom inference configured** — using platform defaults.\n\n"
                "Set up your local backend:\n"
                "  `/inference set --provider ollama --url http://localhost:11434`\n"
                "  `/inference set --provider vllm --url http://your-gpu:8000`\n"
                "  `/inference set --provider openai --key sk-... --url https://api.openai.com/v1`"
            )
        if resp.status_code != 200:
            return f"Failed to fetch config: {resp.status_code}"

        config = resp.json().get("config", {})
        lines = ["**Current Inference Config**"]
        lines.append(f"  Provider: **{config.get('provider', '?')}**")
        if config.get("base_url"):
            lines.append(f"  URL: `{config['base_url']}`")
        if config.get("has_secret_key"):
            lines.append(f"  API Key: configured (stored in vault)")
        mappings = config.get("model_mapping", {})
        if mappings:
            lines.append(f"  Model Mappings:")
            for category, model in mappings.items():
                lines.append(f"    {category} -> `{model}`")
        else:
            lines.append(f"  Model Mappings: none (using provider defaults)")
        lines.append(f"\nTest: `/inference test`  |  Clear: `/inference clear`")
        return "\n".join(lines)

    async def _set(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        provider = "ollama"
        url = None
        api_key = None

        i = 0
        while i < len(args):
            if args[i] in ("--provider", "-p") and i + 1 < len(args):
                provider = args[i + 1].lower()
                i += 2
            elif args[i] in ("--url", "-u", "--base-url") and i + 1 < len(args):
                url = args[i + 1]
                i += 2
            elif args[i] in ("--key", "-k", "--api-key") and i + 1 < len(args):
                api_key = args[i + 1]
                i += 2
            else:
                # Positional: treat as URL if it looks like one
                if args[i].startswith("http"):
                    url = args[i]
                else:
                    provider = args[i]
                i += 1

        if not url:
            defaults = {
                "ollama": "http://localhost:11434",
                "vllm": "http://localhost:8000",
                "openai": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com",
            }
            url = defaults.get(provider)
            if not url:
                return f"Please specify --url for provider '{provider}'"

        body: Dict[str, Any] = {"provider": provider, "base_url": url}
        if api_key:
            body["api_key"] = api_key

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.post("/auth/me/inference-config", json=body)

        if resp.status_code != 200:
            return f"Failed to set config: {resp.status_code} — {resp.text[:300]}"

        lines = [f"**Inference backend configured**"]
        lines.append(f"  Provider: **{provider}**")
        lines.append(f"  URL: `{url}`")
        if api_key:
            lines.append(f"  API Key: stored in vault")
        lines.append(f"\nNow map models: `/inference map chat <model-name>`")
        lines.append(f"Then test: `/inference test`")
        return "\n".join(lines)

    async def _map(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if len(args) < 2:
            return (
                "Usage: `/inference map <category> <model>`\n"
                "Categories: chat, reasoning, coding, vision, general\n"
                "Example: `/inference map reasoning deepseek-r1:14b`"
            )

        category = args[0].lower()
        model = args[1]

        # Fetch current config first
        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/auth/me/inference-config")

        if resp.status_code != 200 or not resp.json().get("config"):
            return "No inference backend configured. Run `/inference set` first."

        config = resp.json()["config"]
        mappings = config.get("model_mapping", {})
        mappings[category] = model

        # Update with new mapping
        body = {
            "provider": config.get("provider", "ollama"),
            "base_url": config.get("base_url"),
            "model_mapping": mappings,
        }

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.post("/auth/me/inference-config", json=body)

        if resp.status_code != 200:
            return f"Failed to update mapping: {resp.status_code}"

        return f"Mapped **{category}** -> `{model}`"

    async def _test(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        # Get current config
        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/auth/me/inference-config")

        if resp.status_code != 200 or not resp.json().get("config"):
            return "No inference backend configured. Run `/inference set` first."

        config = resp.json()["config"]
        provider = config.get("provider", "?")
        base_url = config.get("base_url")

        if not base_url:
            return "No base URL configured."

        # Test connectivity based on provider
        lines = [f"Testing **{provider}** at `{base_url}`..."]

        try:
            if provider == "ollama":
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(f"{base_url}/api/tags")
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    lines.append(f"  Connected! {len(models)} model(s) available:")
                    for m in models[:10]:
                        name = m.get("name", "?")
                        size = m.get("size", 0) / (1024**3)
                        lines.append(f"    `{name}` ({size:.1f}GB)")
                else:
                    lines.append(f"  Connection failed: {resp.status_code}")

            elif provider in ("vllm", "custom"):
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(f"{base_url}/v1/models")
                if resp.status_code == 200:
                    models = resp.json().get("data", [])
                    lines.append(f"  Connected! {len(models)} model(s) served:")
                    for m in models[:10]:
                        lines.append(f"    `{m.get('id', '?')}`")
                else:
                    lines.append(f"  Connection failed: {resp.status_code}")

            elif provider in ("openai", "anthropic"):
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(f"{base_url}/models")
                if resp.status_code in (200, 401):
                    lines.append(f"  Endpoint reachable (auth {'OK' if resp.status_code == 200 else 'required'})")
                else:
                    lines.append(f"  Connection issue: {resp.status_code}")
            else:
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(base_url)
                lines.append(f"  Endpoint responded: {resp.status_code}")

        except httpx.ConnectError:
            lines.append(f"  **Connection refused** — is {provider} running at `{base_url}`?")
        except httpx.TimeoutException:
            lines.append(f"  **Timeout** — endpoint not responding within 10s")
        except Exception as e:
            lines.append(f"  **Error**: {e}")

        # Show model mappings
        mappings = config.get("model_mapping", {})
        if mappings:
            lines.append(f"\nModel mappings:")
            for cat, model in mappings.items():
                lines.append(f"  {cat} -> `{model}`")

        return "\n".join(lines)

    async def _models(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/auth/me/inference-config")

        if resp.status_code != 200 or not resp.json().get("config"):
            return "No inference backend configured. Run `/inference set` first."

        config = resp.json()["config"]
        provider = config.get("provider")
        base_url = config.get("base_url")

        try:
            if provider == "ollama":
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(f"{base_url}/api/tags")
                models = resp.json().get("models", [])
                if not models:
                    return "No models pulled on your Ollama instance.\nPull one: `ollama pull llama3.2:8b`"
                lines = [f"**Models on {provider} ({base_url})**\n"]
                for m in models:
                    name = m.get("name", "?")
                    size = m.get("size", 0) / (1024**3)
                    lines.append(f"  `{name}` — {size:.1f}GB")
                return "\n".join(lines)

            elif provider in ("vllm", "custom"):
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(f"{base_url}/v1/models")
                models = resp.json().get("data", [])
                lines = [f"**Models on {provider} ({base_url})**\n"]
                for m in models:
                    lines.append(f"  `{m.get('id', '?')}`")
                return "\n".join(lines)

            else:
                return f"Model listing not supported for provider '{provider}'"

        except httpx.ConnectError:
            return f"Cannot connect to {base_url} — is {provider} running?"
        except Exception as e:
            return f"Error listing models: {e}"

    async def _clear(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.delete("/auth/me/inference-config")

        if resp.status_code != 200:
            return f"Failed to clear config: {resp.status_code}"

        return "Custom inference config removed. Using platform defaults."

    # ── Quick mode switching (model stacks) ────────────────────────

    # Friendly aliases for common model stacks
    _MODE_ALIASES: Dict[str, str] = {
        "local": "local-reasoning",
        "local-only": "local-reasoning",
        "deepseek": "cloud-dsv4",
        "cloud-deepseek": "cloud-dsv4",
        "dsv4": "cloud-dsv4",
        "cloud": "cloud-offload",
        "cloud-only": "cloud-offload",
        "hybrid": "hybrid-dsv4",
        "dgx": "dgx-hybrid",
        "dgx-deepseek": "hybrid-dsv4",
        "ollama": "ollama-only",
        "ultrathink": "local-ultrathink",
        "gemma4": "local-gemma4",
    }

    async def _mode(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Switch inference mode (model stack) instantly.

        /inference mode                     — Show current mode + presets
        /inference mode local               — Local orchestrator + local reasoning
        /inference mode deepseek            — Local orchestrator + DeepSeek V4 cloud
        /inference mode cloud               — Local orchestrator + cloud reasoning
        /inference mode dgx                 — Local orchestrator + DGX Spark
        /inference mode <stack-name>        — Any model stack by name
        """
        import httpx

        base = _identity_url()

        if not args:
            # Show current mode + available presets
            try:
                async with httpx.AsyncClient(
                    base_url=base, headers=_headers(), timeout=10
                ) as c:
                    resp = await c.get("/model-stacks/active")
                active = resp.json() if resp.status_code == 200 else {}
            except Exception:
                active = {}

            current = active.get("active", active.get("stack", "unknown"))
            desc = active.get("description", "")

            lines = [f"**Current Mode:** `{current}`"]
            if desc:
                lines.append(f"  {desc}")
            lines.append("")
            lines.append("**Quick Switch:**")
            lines.append("  `/inference mode local`     — Orchestrator + local reasoning")
            lines.append("  `/inference mode deepseek`  — Orchestrator + DeepSeek V4 cloud")
            lines.append("  `/inference mode cloud`     — Orchestrator + cloud offload")
            lines.append("  `/inference mode dgx`       — Orchestrator + DGX Spark")
            lines.append("  `/inference mode ollama`    — Ollama only (no GPU)")
            lines.append("")
            lines.append("  `/inference stacks` — List all available stacks")
            return "\n".join(lines)

        target = args[0].lower()
        stack_name = self._MODE_ALIASES.get(target, target)

        # Preview first
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=_headers(), timeout=10
            ) as c:
                preview = await c.get(f"/model-stacks/{stack_name}/preview")
            if preview.status_code == 404:
                return (
                    f"Unknown mode: `{target}`\n"
                    f"Run `/inference stacks` to see available options."
                )
        except Exception:
            pass

        # Switch
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=_headers(), timeout=30
            ) as c:
                resp = await c.post(
                    "/model-stacks/switch",
                    json={"stack": stack_name},
                )
            if resp.status_code != 200:
                return f"Switch failed: {resp.status_code} — {resp.text[:300]}"

            data = resp.json()
            lines = [f"**Switched to `{stack_name}`**"]
            if data.get("description"):
                lines.append(f"  {data['description']}")
            actions = data.get("actions", [])
            if actions:
                lines.append("  Actions:")
                for a in actions[:5]:
                    lines.append(f"    - {a}")

            # Sync cloud_mode to user inference config so all surfaces see it
            _cloud_mode = "local_first"
            if "cloud" in stack_name or "dsv4" in stack_name:
                _cloud_mode = "cloud_first"
            try:
                async with httpx.AsyncClient(
                    base_url=base, headers=_headers(), timeout=10
                ) as c:
                    await c.put(
                        "/config/me/inference",
                        json={"cloud_mode": _cloud_mode},
                    )
                lines.append(f"  Inference config synced: cloud_mode={_cloud_mode}")
            except Exception:
                pass  # best-effort sync

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach Genesis — is it running?"
        except Exception as e:
            return f"Switch failed: {e}"

    async def _stacks(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """List all available model stacks."""
        import httpx

        try:
            async with httpx.AsyncClient(
                base_url=_identity_url(), headers=_headers(), timeout=10
            ) as c:
                resp = await c.get("/model-stacks")
            if resp.status_code != 200:
                return f"Failed to list stacks: {resp.status_code}"

            data = resp.json()
            stacks = data.get("stacks", [])
            active = data.get("active", "")

            lines = ["**Available Model Stacks**\n"]
            for s in stacks:
                name = s.get("name", "?")
                desc = s.get("description", "")
                marker = " **(active)**" if name == active else ""
                gpu = s.get("requires_gpu", False)
                cloud = "cloud" in name or "dsv4" in name
                icon = "Cloud" if cloud else ("GPU" if gpu else "CPU")
                lines.append(f"  `{name}`{marker} — {desc} [{icon}]")

            lines.append(f"\nSwitch: `/inference mode <name>`")
            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach Genesis — is it running?"
        except Exception as e:
            return f"Error: {e}"

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return (
            "**Inference Backend Configuration**\n\n"
            "**Quick Mode Switch:**\n"
            "  `/inference mode`                — Show current mode\n"
            "  `/inference mode local`          — Local orchestrator + local reasoning\n"
            "  `/inference mode deepseek`       — Local orchestrator + DeepSeek V4 cloud\n"
            "  `/inference mode cloud`          — Local orchestrator + cloud offload\n"
            "  `/inference stacks`              — List all model stacks\n\n"
            "**Custom Backend:**\n"
            "  `/inference set --provider ollama --url http://localhost:11434`\n"
            "  `/inference set --provider vllm --url http://my-gpu:8000`\n"
            "  `/inference set --provider openai --key sk-... --url https://api.openai.com/v1`\n\n"
            "**Model Mapping:**\n"
            "  `/inference map chat llama3.2:8b`\n"
            "  `/inference map reasoning deepseek-r1:14b`\n"
            "  `/inference map coding qwen2.5-coder:7b`\n\n"
            "**Manage:**\n"
            "  `/inference` or `/inference show` — Current config\n"
            "  `/inference test` — Test connectivity\n"
            "  `/inference models` — List models on your backend\n"
            "  `/inference clear` — Remove config, use platform defaults"
        )

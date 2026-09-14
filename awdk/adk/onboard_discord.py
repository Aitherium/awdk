"""Automated onboarding: turn your awdk agent into a Discord bot.

``adk onboard --discord`` walks a user through deploying their own agent as a
self-service Discord bot. Every step is an automated check that can fail:

  1. Install the agent pack they want (``adk install pack:<name>``).
  2. Read the bot token (``--token`` / ``DISCORD_BOT_TOKEN`` / prompt).
  3. **Validate the token live** against Discord's API (``GET /users/@me``) —
     a real gate; a revoked/typo'd token fails here with a clean message.
  4. **Print the invite link**, derived from the token's application id, so
     the user adds the bot to their server.
  5. Verify the agent identity + registered tools resolve (the "identity did
     not resolve" gate).
  6. Optionally ``--run``: launch the bot (the built-in ``DiscordAdapter`` when
     entitled, else a hand-rolled ``discord.py`` client that works on any tier).

This is the user-facing automation; ``awskills/tools/discord-agent-bot.py``
is the standalone equivalent for when the SDK command isn't available.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import sys

DISCORD_API = "https://discord.com/api/v10"
INVITE = "https://discord.com/oauth2/authorize"
LIMIT = 2000  # Discord message length cap

_LICENSE_HINT = "Upgrade at api.aitherium.com/portal/marketplace/packs"


# ── Token helpers ───────────────────────────────────────────────────────────


def derive_application_id(token: str) -> str:
    """Discord bot tokens are ``<application_id>.<timestamp>.<hmac>``."""
    return (token or "").strip().split(".")[0]


def invite_url(token: str, permissions: int = 0) -> str:
    """Invite link that adds the bot to a server (minimal ``bot`` scope)."""
    app_id = derive_application_id(token)
    return f"{INVITE}?client_id={app_id}&scope=bot&permissions={permissions}"


async def validate_token(token: str) -> dict:
    """Live-check the token against Discord's API.

    Returns ``{"ok": True, id, username, bot}`` or ``{"ok": False, status, error}``.
    This is the automated gate that turns a typo'd/revoked token into a clear
    failure instead of a silent bot that never connects.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
    except Exception as exc:  # noqa: BLE001 — network failure is a gate failure
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}

    if r.status_code == 200:
        d = r.json()
        return {
            "ok": True,
            "id": d.get("id"),
            "username": d.get("username"),
            "bot": bool(d.get("bot")),
        }
    return {"ok": False, "status": r.status_code, "error": (r.text or "")[:200]}


# ── Agent building ──────────────────────────────────────────────────────────


def _chunk(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


def build_agent(identity: str, tools_module: str | None = None):
    """Construct an ``AitherAgent`` for *identity*, registering pack tools."""
    from adk.agent import AitherAgent
    from adk.tools import get_global_registry

    if tools_module:
        importlib.import_module(tools_module)
    registry = get_global_registry()
    tools = [registry] if registry.list_tools() else None
    return AitherAgent(identity, tools=tools, load_packs=True)


async def agent_reply(agent, text: str) -> str:
    resp = await agent.chat(text)
    content = getattr(resp, "content", None)
    return content if isinstance(content, str) and content.strip() else str(resp)


# ── Discord client (works on any tier) ──────────────────────────────────────


def make_discord_client(agent):
    """Hand-rolled discord.py client: DMs + @mentions -> agent, chunked replies."""
    import discord

    class _Client(discord.Client):
        async def on_ready(self) -> None:
            print(f"  ✅ online as {self.user} — DM me or @mention me in a channel")

        async def on_message(self, message) -> None:  # noqa: N802 (discord.py hook)
            if message.author == self.user:
                return
            is_dm = message.guild is None
            is_mention = bool(self.user) and self.user in message.mentions
            if not (is_dm or is_mention):
                return
            text = message.content
            if is_mention and self.user:
                text = re.sub(rf"<@!?{self.user.id}>\s*", "", text).strip()
            reply = await agent_reply(agent, text)
            if reply:
                for chunk in _chunk(reply):
                    await message.channel.send(chunk)

    intents = discord.Intents.default()
    intents.message_content = True
    return _Client(intents=intents)


async def _run_bot(token: str, identity: str, tools_module: str | None) -> int:
    """Launch the bot: built-in DiscordAdapter first, hand-rolled fallback."""
    from adk.agent import AitherAgent
    from adk.channels import DiscordAdapter

    agent = build_agent(identity, tools_module)

    # 1) Built-in adapter (paid-tier ``channels`` capability).
    try:

        async def handler(_platform, _channel_id, _user_id, text):
            return await agent_reply(agent, text)

        adapter = DiscordAdapter(token=token, on_message=handler)
    except Exception as exc:  # noqa: BLE001 — LicenseError/ImportError -> fallback
        print(f"  (built-in DiscordAdapter unavailable: {exc})")
        adapter = None
    if adapter is not None:
        await adapter.start()
        await asyncio.Event().wait()
        return 0

    # 2) Hand-rolled discord.py client — any tier.
    client = make_discord_client(agent)
    try:
        await client.start(token)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 — fail closed with a clean message
        name = type(exc).__name__
        print(f"✗ could not connect to Discord ({name}): {exc}", file=sys.stderr)
        if name == "LoginFailure" or "Improper token" in str(exc):
            print("  → the bot token is invalid or revoked. Reset it in the "
                  "Discord Developer Portal and re-export DISCORD_BOT_TOKEN.",
                  file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0


# ── The onboarding gate (steps 2-5) ─────────────────────────────────────────


def check(token: str, identity: str, tools_module: str | None = None) -> int:
    """Validate the token live + verify identity/tools. Exit non-zero on failure."""
    if not token:
        print("✗ no bot token — pass --token or set DISCORD_BOT_TOKEN.", file=sys.stderr)
        return 1

    print(f"  validating Discord token …")
    result = asyncio.run(validate_token(token))
    if not result.get("ok"):
        print(f"✗ Discord rejected the token (HTTP {result.get('status')}): "
              f"{result.get('error')}", file=sys.stderr)
        if result.get("status") == 401:
            print("  → the token is invalid or revoked. Reset it in the Discord "
                  "Developer Portal.", file=sys.stderr)
        return 2
    print(f"  ✅ bot token valid — logged in as {result.get('username')} "
          f"(id {result.get('id')})")

    url = invite_url(token)
    print(f"  invite link:  {url}")
    print("    open it, pick a server, Authorize. Then come back and --run.")

    # Identity + tools gate.
    try:
        from adk.identity import load_identity
    except ImportError:  # pragma: no cover
        from adk.identities import load_identity  # type: ignore[attr-defined]
    ident = load_identity(identity)
    resolved = bool(ident.description or ident.system_prompt or ident.skills)
    print(f"  identity: {ident.name} | role: {ident.role}"
          f"{' | skills: ' + ', '.join(ident.skills) if ident.skills else ''}")
    if not resolved:
        print(f"⚠ identity '{identity}' did not resolve — install the pack "
              f"(adk install pack:<name>) and re-run, or pass --identity that exists.",
              file=sys.stderr)
        return 2

    tools = []
    if tools_module:
        importlib.import_module(tools_module)
    try:
        from adk.tools import get_global_registry
        tools = [t.name for t in get_global_registry().list_tools()]
    except Exception:  # noqa: BLE001
        pass
    print(f"  tools: {', '.join(tools[:12]) if tools else '(none registered)'}"
          + (f" ({len(tools)} total)" if tools else ""))
    return 0


# ── CLI entry (used by ``adk onboard --discord``) ───────────────────────────


def onboard_discord(args) -> int:
    """``adk onboard --discord`` — the automated onboarding chain."""
    identity = getattr(args, "identity", None) or os.environ.get("ADK_AGENT", "aither")
    token = getattr(args, "token", None) or os.environ.get("DISCORD_BOT_TOKEN", "")
    tools_module = getattr(args, "tools_module", None)
    pack = getattr(args, "pack", None)
    do_run = bool(getattr(args, "run", False))

    print("\n  Deploy your awdk agent as a Discord bot (automated)\n")

    # Step 1: install the pack the user chose.
    if pack and not getattr(args, "skip_pack_install", False):
        print(f"  installing pack: {pack} …")
        try:
            from adk.cli import cmd_install
            from types import SimpleNamespace
            code = cmd_install(SimpleNamespace(target=f"pack:{pack}", install_command=None))
            if code:
                return code
        except Exception as exc:  # noqa: BLE001
            print(f"✗ pack install failed: {exc}", file=sys.stderr)
            return 1

    # Steps 2-5: the onboarding gate (token validate + invite + identity/tools).
    gate = check(token, identity, tools_module)
    if gate:
        return gate

    # Step 6: launch when asked.
    if do_run:
        if not token:
            print("✗ --run needs a token.", file=sys.stderr)
            return 1
        print("\n  starting the bot (Ctrl+C to stop)…\n")
        return asyncio.run(_run_bot(token, identity, tools_module))

    print("\n  Next: open the invite link, add the bot to a server, then run:")
    print(f"    adk onboard --discord --identity {identity} --run"
          + (f" --pack {pack}" if pack else ""))
    return 0

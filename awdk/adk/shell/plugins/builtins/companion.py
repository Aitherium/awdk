"""
Companion Plugin for AitherShell
==================================

Manage AI companion personas, persistent memory, and conversation recall.

Usage:
    /companion                      — Show active companion
    /companion list                 — List available personas
    /companion set <name>           — Switch active companion
    /companion create <name>        — Create a new companion profile
    /companion customize <trait> <1-10> — Adjust personality trait
    /companion remember <text>      — Store a memory
    /companion memories [category]  — View stored memories
    /companion recall <query>       — Search conversation history
    /companion context              — Show assembled LLM context
    /companion safety               — Show current content tier
    /companion safety <level>       — Set content tier

Aliases: /persona, /will
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


class CompanionPlugin(SlashCommand):
    name: str = "companion"
    aliases: List[str] = ["persona", "will"]
    description: str = "Manage AI companions, memory, and conversation recall"
    category: str = "ai"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='companion',
            description='Manage AI companions, memory, and conversation recall',
            aliases=['persona', 'will'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._active(ctx)

        sub = args[0].lower()
        dispatch = {
            "list": self._list,
            "set": self._set,
            "switch": self._set,
            "create": self._create,
            "new": self._create,
            "customize": self._customize,
            "trait": self._customize,
            "remember": self._remember,
            "memories": self._memories,
            "recall": self._recall,
            "search": self._recall,
            "context": self._context,
            "safety": self._safety,
            "tier": self._safety,
            "local_vault": self._local_vault,
            "vault": self._local_vault,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)

        # Default: treat as companion name to switch to
        return await self._set(args, ctx)

    async def _active(self, ctx: Dict[str, Any]) -> str:
        import httpx

        user_id = ctx.get("config", {}).get("user_id", "default")
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/companions/list", params={"user_id": user_id})

        if resp.status_code != 200:
            return "No active companion. Use `/companion list` to browse, `/companion set <name>` to choose."

        data = resp.json()
        companions = data.get("companions", [])
        active = [c for c in companions if c.get("active")]
        if active:
            comp = active[0]
            lines = [f"🧠 **Active Companion: {comp.get('name', '?')}**"]
            if comp.get("display_name"):
                lines.append(f"  Display: {comp['display_name']}")
            if comp.get("personality_summary"):
                lines.append(f"  {comp['personality_summary']}")
            return "\n".join(lines)

        return f"📋 {len(companions)} companions available. Use `/companion set <name>` to activate."

    async def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        # List base personas
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/companions/personas")

        if resp.status_code != 200:
            return f"❌ Failed to list personas: {resp.status_code}"

        data = resp.json()
        personas = data.get("personas", [])
        if not personas:
            return "No personas configured."

        lines = ["👥 **Available Personas**\n"]
        for p in personas:
            name = p.get("name", "?")
            desc = p.get("description", "")
            tier = p.get("default_tier", "")
            tier_str = f" [{tier}]" if tier else ""
            lines.append(f"  **{name}**{tier_str} — {desc}")
        return "\n".join(lines)

    async def _set(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/companion set <persona_name>`"

        name = args[0]
        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        body = {"persona_name": name, "user_id": user_id}
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.post("/companions/customize", json=body)

        if resp.status_code != 200:
            return f"❌ Failed to set companion: {resp.status_code} — {resp.text[:200]}"

        return f"✅ Active companion set to **{name}**"

    async def _create(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/companion create <name>` — creates a new companion profile"

        name = args[0]
        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        body = {
            "persona_name": name,
            "user_id": user_id,
            "display_name": name.replace("-", " ").title(),
        }

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.post("/companions/customize", json=body)

        if resp.status_code != 200:
            return f"❌ Failed to create companion: {resp.status_code}"

        return f"✅ Companion **{name}** created. Customize: `/companion customize <trait> <1-10>`"

    async def _customize(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        valid_traits = [
            "warmth", "humor", "formality", "curiosity",
            "assertiveness", "empathy", "creativity", "patience"
        ]

        if len(args) < 2:
            return (f"Usage: `/companion customize <trait> <1-10>`\n"
                    f"Traits: {', '.join(valid_traits)}")

        trait = args[0].lower()
        if trait not in valid_traits:
            return f"Unknown trait `{trait}`. Valid: {', '.join(valid_traits)}"

        try:
            value = int(args[1])
            if not 1 <= value <= 10:
                raise ValueError
        except ValueError:
            return "Value must be 1-10"

        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        body = {
            "user_id": user_id,
            "personality_overrides": {trait: value / 10.0},
        }

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.post("/companions/customize", json=body)

        if resp.status_code != 200:
            return f"❌ Failed to update: {resp.status_code}"

        return f"✅ **{trait}** set to {value}/10"

    async def _remember(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/companion remember <something to remember>`"

        content = " ".join(args)
        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        body = {
            "user_id": user_id,
            "content": content,
            "category": "user_shared",
            "importance": 0.8,
        }

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.post("/companions/memory", json=body)

        if resp.status_code != 200:
            return f"❌ Failed to store memory: {resp.status_code}"

        return f"💾 Remembered: *{content[:80]}*"

    async def _memories(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        params: Dict[str, Any] = {"user_id": user_id, "limit": 15}
        if args:
            params["category"] = args[0]

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/companions/memories", params=params)

        if resp.status_code != 200:
            return f"❌ Failed to fetch memories: {resp.status_code}"

        data = resp.json()
        memories = data.get("memories", [])
        if not memories:
            return "No memories stored. Use `/companion remember <text>` to add one."

        lines = [f"🧠 **{len(memories)} Memories**\n"]
        for m in memories:
            cat = m.get("category", "")
            content = m.get("content", "?")[:100]
            importance = m.get("importance", 0)
            star = "⭐" if importance >= 0.8 else "  "
            lines.append(f"  {star} [{cat}] {content}")
        return "\n".join(lines)

    async def _recall(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/companion recall <query>` — search conversation history"

        query = " ".join(args)
        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        params = {"user_id": user_id, "query": query, "limit": 10}
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/companions/recall", params=params)

        if resp.status_code != 200:
            return f"❌ Recall failed: {resp.status_code}"

        data = resp.json()
        results = data.get("results", [])
        if not results:
            return f"No conversations found matching *{query}*"

        lines = [f"🔍 **Recall: {query}**\n"]
        for r in results:
            topic = r.get("topic", "?")
            ts = r.get("timestamp", "")[:10]
            relevance = r.get("relevance", 0)
            lines.append(f"  📝 **{topic}** ({ts}) — relevance: {relevance:.0%}")
            if r.get("summary"):
                lines.append(f"     {r['summary'][:120]}")
        return "\n".join(lines)

    async def _context(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        user_id = "default"
        if hasattr(ctx.get("config"), "user_id"):
            user_id = ctx["config"].user_id

        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.get("/companions/context", params={"user_id": user_id})

        if resp.status_code != 200:
            return f"❌ Context failed: {resp.status_code}"

        data = resp.json()
        lines = ["🧩 **Companion Context**\n"]
        if data.get("companion_name"):
            lines.append(f"  Companion: **{data['companion_name']}**")
        if data.get("content_tier"):
            lines.append(f"  Content tier: `{data['content_tier']}`")
        if data.get("memory_count"):
            lines.append(f"  Memories loaded: {data['memory_count']}")
        prompt_len = len(data.get("system_prompt", ""))
        if prompt_len:
            lines.append(f"  System prompt: {prompt_len} chars")
        return "\n".join(lines)

    async def _safety(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            # Show current level
            async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
                resp = await c.get("/safety/config/levels")
            if resp.status_code != 200:
                return f"❌ Failed to get safety levels: {resp.status_code}"
            data = resp.json()
            levels = data.get("levels", [])
            current = data.get("current", "")
            lines = ["🛡️ **Content Tiers**\n"]
            for lv in levels:
                name = lv.get("name", "?")
                desc = lv.get("description", "")
                marker = " ◀ active" if name == current else ""
                lines.append(f"  `{name}` — {desc}{marker}")
            return "\n".join(lines)

        # Set level
        level = args[0]
        async with httpx.AsyncClient(base_url=_genesis_url(), headers=_headers(), timeout=15) as c:
            resp = await c.put("/safety/config/user", json={"content_tier": level})

        if resp.status_code != 200:
            return f"❌ Failed to set tier: {resp.status_code} — {resp.text[:200]}"

        return f"✅ Content tier set to **{level}**"

    async def _local_vault(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Manage the local private companion vault (operator-blind encryption).

        Commands:
            /companion local_vault status         — show vault status
            /companion local_vault store <name> <path>  — store persona from file
            /companion local_vault list           — list stored personas
            /companion local_vault level [tier]   — get/set safety tier
        """
        if not args:
            return self._get_vault_help()

        try:
            from adk.private_companion import get_companion_vault
        except ImportError:
            return (
                "❌ Private companion vault requires cryptography package.\n"
                "Install with: `pip install cryptography`"
            )

        vault = get_companion_vault()
        if not vault:
            return "❌ Failed to initialize private vault"

        sub = args[0].lower()
        if sub == "status":
            status = vault.vault_status()
            lines = ["📦 **Private Companion Vault**\n"]
            lines.append(f"  Location: {status.get('lockbox_dir')}")
            lines.append(f"  Personas: {status.get('persona_count', 0)}")
            if status.get('personas'):
                lines.append(f"  Stored: {', '.join(status['personas'])}")
            lines.append(
                f"  Safety level: {status.get('safety_level', 'not set')}"
            )
            lines.append("\n✅ All data encrypted locally; operator-blind")
            return "\n".join(lines)

        elif sub == "list":
            personas = vault.list_personas()
            if not personas:
                return "No personas stored. Use `/companion vault store` to add one."
            lines = ["👥 **Stored Personas**\n"]
            for name in personas:
                persona = vault.get_persona(name)
                if persona:
                    lines.append(
                        f"  **{name}** [{persona.safety_level}] — {persona.description or '(no desc)'}"
                    )
            return "\n".join(lines)

        elif sub == "level":
            if len(args) == 1:
                # Show current level
                level = vault.get_safety_level()
                return f"Safety level: `{level or 'professional (default)'}`"
            else:
                # Set level
                level = args[1]
                if level not in ("professional", "casual", "unrestricted", "explicit"):
                    return (
                        f"Invalid tier `{level}`. "
                        f"Valid: professional, casual, unrestricted, explicit"
                    )
                vault.set_safety_level(level)
                return f"✅ Safety level set to `{level}`"

        elif sub == "store":
            if len(args) < 3:
                return "Usage: `/companion vault store <name> <path>` — store persona from file"
            name = args[1]
            path = " ".join(args[2:])  # path might have spaces
            import os
            if not os.path.exists(path):
                return f"❌ File not found: {path}"
            try:
                with open(path, "r") as f:
                    content = f.read()
                vault.store_persona(name, content, safety_level="unrestricted")
                return f"✅ Stored persona **{name}** from {path}"
            except Exception as e:
                return f"❌ Failed to store persona: {e}"

        elif sub in ("backup", "restore"):
            # The passphrase is read from the env (never a shell arg → never logged in
            # history). It is the ONLY thing that can decrypt a portable backup — keep
            # it safe; lose it and the backup is unrecoverable (that is the point).
            import os
            passphrase = os.environ.get("AITHER_COMPANION_BACKUP_PASSPHRASE", "")
            if not passphrase:
                return (
                    "🔑 Set a backup passphrase first (kept out of shell history):\n"
                    "  `export AITHER_COMPANION_BACKUP_PASSPHRASE='…'`\n"
                    "It encrypts the portable backup so it restores on any machine but "
                    "stays operator-blind. Lose it and the backup can't be recovered."
                )
            if len(args) < 2:
                return f"Usage: `/companion vault {sub} <file>`"
            path = " ".join(args[1:])
            if sub == "backup":
                try:
                    out = vault.export_backup_to_file(path, passphrase)
                    return (
                        f"✅ Portable backup written to `{out}`\n"
                        "Move it to another machine and `restore` with the SAME passphrase."
                    )
                except Exception as e:
                    return f"❌ Backup failed: {e}"
            else:  # restore
                overwrite = "--overwrite" in args or "--force" in args
                clean = [a for a in args[1:] if a not in ("--overwrite", "--force")]
                path = " ".join(clean)
                try:
                    result = vault.import_backup_from_file(
                        path, passphrase, overwrite=overwrite)
                    return (
                        f"✅ Restored {result['count']} persona(s): "
                        f"{', '.join(result['restored']) or '(none)'}\n"
                        "Re-pinned to this machine; your companion is back."
                    )
                except Exception as e:
                    hint = "" if "overwrite" not in str(e) else " (add `--overwrite` to replace)"
                    return f"❌ Restore failed: {e}{hint}"

        else:
            return self._get_vault_help()

    def _get_vault_help(self) -> str:
        return """🔐 **Private Companion Vault** (operator-blind)

| Command | Description |
|---------|-------------|
| `/companion vault status` | Show vault status |
| `/companion vault list` | List stored personas |
| `/companion vault level [tier]` | Get/set safety tier |
| `/companion vault store <name> <path>` | Store persona from file |
| `/companion vault backup <file>` | Write a portable, passphrase-encrypted backup |
| `/companion vault restore <file> [--overwrite]` | Restore a backup onto this machine |

**Safety Tiers:** professional, casual, unrestricted, explicit

**Backup/restore** move your companion between machines or recover after disk loss.
Set `AITHER_COMPANION_BACKUP_PASSPHRASE` first — it encrypts the portable bundle so
it restores anywhere yet stays operator-blind. Lose it and the backup is unrecoverable.

All data is encrypted locally with your machine key. The operator never sees it.
"""

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()

    def get_help(self) -> str:
        return """🧠 **Companion Management**

| Command | Description |
|---------|-------------|
| `/companion` | Show active companion |
| `/companion list` | Available personas |
| `/companion set <name>` | Switch companion |
| `/companion create <name>` | Create new companion |
| `/companion customize <trait> <1-10>` | Adjust personality |
| `/companion remember <text>` | Store a memory |
| `/companion memories` | View memories |
| `/companion recall <query>` | Search conversations |
| `/companion context` | Show LLM context |
| `/companion safety [level]` | View/set content tier |

**Traits:** warmth, humor, formality, curiosity, assertiveness, empathy, creativity, patience
"""

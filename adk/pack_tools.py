"""Agent tools to find and add a capability mid-task: ``search_packs`` and ``install_pack``.

An agent that hits a task it has no tool for can search every add-on source and install
one without a restart. Both tools go through the ONE resolver every other surface uses
(:mod:`adk.addons`: awpack, then Library packs, then awnode extensions; ``skill:<name>``
for awskills), so the agent installs exactly what ``adk pack install`` would, through the
owning brick, and the same lock row (:mod:`adk.addon_lock`) records source + sha256.
A Library pack is then mounted on the live agent (:meth:`AitherAgent.activate_pack`).

Trust: an ``official`` add-on installs without a prompt. Anything else -- a community
listing or shelf, a local file, an id no source vouches for -- is gated by the approval
system (``adk.approval.register_arg_gate``): the turn pauses and the owner sees an
Allow/Deny card. The gate is judged before the durable allowlist, so it cannot be
pre-approved away.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("adk.pack_tools")

__all__ = ["pack_trust", "needs_install_approval", "register_pack_tools", "TOOL_NAMES"]

TOOL_NAMES = ("search_packs", "install_pack")


def pack_trust(ref: str) -> str:
    """``official`` | ``community`` | ``local``; anything unresolvable is ``community``."""
    from adk.addons import trust_of
    return trust_of(ref)


def needs_install_approval(args: dict) -> bool:
    """Approval predicate: True (ask the owner) unless the add-on is first-party."""
    return pack_trust(str((args or {}).get("pack_id") or "")) != "official"


def register_pack_tools(agent: Any) -> int:
    """Register ``search_packs`` and ``install_pack`` on *agent*. Returns the count."""
    from adk.approval import register_arg_gate
    register_arg_gate("install_pack", needs_install_approval)

    def search_packs(query: str, limit: int = 10) -> str:
        """Search installable add-ons (packs, extensions, skills) by name, summary or tag.

        Use this when the task needs a tool you do not have. Each result carries its kind,
        trust tier (official | community | local) and whether it is installed.
        query: words describing the capability, e.g. "postgres" or "pdf".
        limit: max results (default 10).
        """
        from adk import addons
        q = (query or "").strip()
        if not q:
            return json.dumps({"error": "query is required"})
        hits, down = addons.search(q, kinds=(*addons.SOURCES, "skill"))
        n = max(1, min(int(limit or 10), 50))
        return json.dumps({"query": q, "count": len(hits[:n]),
                           "packs": [h.to_dict() for h in hits[:n]],
                           "unavailable": down})

    def install_pack(pack_id: str) -> str:
        """Install an add-on and, for a tool pack, mount its tools on yourself now.

        First-party add-ons install directly; anything third-party pauses for the owner's
        approval first. Use the id from search_packs; prefix skill:<name> for a skill.
        pack_id: the add-on id (or <kind>:<id>, or community:<listing-id>).
        """
        from adk import addons
        pid = (pack_id or "").strip()
        trust = pack_trust(pid)
        # Reaching here past a non-official trust means the owner already approved it.
        res = addons.install(pid, allow_community=True)
        if not res.ok:
            already = "already installed" in res.detail
            if not already:
                return json.dumps({"ok": False, "pack": res.id or pid, "trust": trust,
                                   "kind": res.kind, "error": res.detail})
        out: "dict[str, Any]" = {"ok": True, "pack": res.id, "kind": res.kind,
                                 "trust": trust, "detail": res.detail, "lock": res.record}
        if res.kind == "library":
            activate = getattr(agent, "activate_pack", None)
            if activate is None:
                out["activation"] = {"error": "this agent cannot mount packs at runtime"}
            else:
                out["activation"] = activate(res.id)
                if isinstance(out["activation"], dict) and out["activation"].get("error"):
                    out["ok"] = False
        else:
            out["activation"] = {"note": f"a {res.kind} add-on loads on the next start"
                                 + (f"; next: {res.next}" if res.next else "")}
        return json.dumps(out, default=str)

    agent._tools.register(search_packs)
    agent._tools.register(install_pack)
    return 2

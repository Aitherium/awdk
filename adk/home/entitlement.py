"""Agent Home licensing -- what the ``agent-home`` pack unlocks, and installing it.

Free: set up an agent, pick a model and harness, edit the persona, join a game
and play it (observe / act / chat) for a session.

Pack ``agent-home`` (one-time purchase): learning that PERSISTS across game
sessions (the agent remembers and improves), world-model enrollment of a game
room, and more than one agent in a room at once.

The check is ``adk.licensing``'s own ``is_pack_available("agent-home")`` over
every verified license: the ACCOUNT license (``adk home signin`` / ``adk login``
sync it from the Aitherium account the pack was bought with) plus any OFFLINE
license installed with ``adk home license`` (kept under ``~/.aither/licenses/``).
As adk.licensing says, a local check exists for clear errors and good UX; it is
not the only wall.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List

PACK_ID = "agent-home"
SHOP_URL = "https://aitherium.com/shop/agent-home"

PREMIUM_FEATURES: Dict[str, str] = {
    "game_learning": "learning that carries across game sessions",
    "multi_agent": "more than one agent at a time",
}


def _manager() -> Any:
    from adk.licensing import LicenseManager

    return LicenseManager()  # re-resolved each call: a new license applies at once


def has_pack() -> bool:
    mgr = _manager()
    if not mgr._enforced():
        return True
    return bool(mgr.is_pack_available(PACK_ID))


def status() -> Dict[str, Any]:
    mgr = _manager()
    lic = mgr.license
    owned = has_pack()
    return {"pack": PACK_ID, "owned": owned, "tier": lic.tier.value,
            "source": lic.source, "packs": list(lic.packs),
            "features": {k: owned for k in PREMIUM_FEATURES},
            "buy": None if owned else SHOP_URL}


def require(feature: str) -> None:
    """Raise ``adk.licensing.LicenseError`` unless the pack is owned."""
    from adk.licensing import LicenseError

    if feature not in PREMIUM_FEATURES:
        raise ValueError(f"unknown Agent Home feature {feature!r}")
    if has_pack():
        return
    raise LicenseError(
        f"{PREMIUM_FEATURES[feature]} needs the Agent Home kit (license pack "
        f"'{PACK_ID}'). Get it at {SHOP_URL}, then run `adk home signin` "
        "(or, offline, `adk home license <license-file>`).")


def license_path() -> Path:
    return Path(os.environ.get("AITHER_LICENSE_FILE")
                or (Path.home() / ".aither" / "license.json"))


def _parse_envelope(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("empty license")
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("license is neither JSON nor base64 JSON") from exc
    env = json.loads(text)
    if not isinstance(env, dict) or "payload" not in env or "signature" not in env:
        raise ValueError("license must be a {payload, signature} envelope")
    return env


class LicenseWouldDropPacks(ValueError):
    """Kept for importers. Never raised any more: offline licenses are kept side by
    side under ``~/.aither/licenses/`` and adk.licensing unions their packs, so
    installing one license can no longer switch another one's packs off."""

    def __init__(self, packs: List[str]):
        self.packs = list(packs)
        super().__init__(f"would drop packs: {', '.join(self.packs)}")


def install_license(text_or_path: str, replace: bool = False) -> Dict[str, Any]:
    """Verify a pasted license (text, base64 or a file path) and keep it offline.

    Refuses anything whose signature does not verify (nothing is written). A good
    license is saved as its own file under ``~/.aither/licenses/`` -- beside the
    account license and any other offline license, never over them. ``replace`` is
    accepted for compatibility and has nothing left to do.
    """
    from adk.licensing import install_offline_license, parse_license_text

    del replace
    candidate = Path(text_or_path.strip().strip('"'))
    try:
        is_file = len(text_or_path) < 1024 and candidate.is_file()
    except OSError:
        is_file = False
    raw = candidate.read_text(encoding="utf-8") if is_file else text_or_path
    env = parse_license_text(raw)
    lic, dest = install_offline_license(env)
    return {"saved": str(dest), "tier": lic.tier.value, "packs": list(lic.packs),
            "agent_home": PACK_ID in lic.packs or "*" in lic.entitlements.named_agents,
            "backup": None, "packs_no_longer_active": []}

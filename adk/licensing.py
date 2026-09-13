"""adk.licensing — the open-core entitlement keystone.

This module is the single source of truth for what a locally-installed
``awdk`` runtime is allowed to do **without** phoning home.  It is
deliberately shipped in the published PyPI wheel (unlike the internal moat at
the platform's packages tree) and contains **no secrets** — only a public
verification key.  The portal signs entitlements with its private key; this
module merely *verifies* them.

Design rules (do not regress):

1. **Fail-closed, not fail-open.**  No license / an unverifiable license / an
   expired license all resolve to the free :data:`Tier.COMMUNITY` tier.  An
   unsigned ``license.json`` never grants premium capabilities.
2. **The free tier is genuinely useful.**  COMMUNITY ships a real agent with
   typed memory, graph memory, code graph, the essential tools and ReAct.  The
   gates only fence off the *scale* capabilities (fleet, channels, cron,
   proactive auto-neurons, swarm, packs) and reasoning-tier effort.
3. **AitherOS's own deployments are never gated.**  ``AITHER_TENANT_SLUG=aitherium``
   (or a verified internal license) resolves to :data:`Tier.INTERNAL`, which
   allows everything.  This keeps custom apps and built-in tools working unchanged.
4. **The real money-gate is server-side.**  Premium identities/skills/tools are
   not in the wheel and are delivered by Genesis ``/v1/packs/download`` behind a
   402.  Local checks exist to give *clear errors and good UX*, not to be the
   only wall.

Resolution order (first hit wins):
    AITHER_LICENSE_ENFORCE=0          -> enforcement disabled (returns allow)
    AITHER_TENANT_SLUG=aitherium      -> INTERNAL
    AITHER_LICENSE_KEY=<b64 payload>  -> verified tier (or COMMUNITY if invalid)
    ~/.aither/license.json            -> verified tier (or COMMUNITY if invalid)
    (nothing)                         -> COMMUNITY
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.licensing")

PORTAL_PACKS_URL = "api.aitherium.com/portal/marketplace/packs"

# Ed25519 public key (hex, 32 bytes) used to verify portal-signed licenses.
# The matching PRIVATE key lives only in the Aitherium platform vault
# (AITHER_LICENSE_SIGNING_KEY) and is NEVER shipped.  Override at runtime with
# AITHER_LICENSE_PUBLIC_KEY=<hex> for self-hosted/sovereign signing roots.
_LICENSE_PUBLIC_KEY_HEX = (
    "1a468a332d6cfc5378edf7083b6d845bcfdf141fbce28e6dbe521dd6b84e233f"
)


class LicenseError(RuntimeError):
    """Raised when a gated capability is used without entitlement."""


class Tier(str, Enum):
    """License tiers, ascending.  Each tier includes everything below it."""

    COMMUNITY = "community"        # free, local, single agent
    STARTER = "starter"           # + fleet, a few premium identities, channels
    BUILDER = "builder"          # + cron, all tools, skill/tool packs
    PROFESSIONAL = "professional"  # + reasoning effort, swarm, custom agents
    ENTERPRISE = "enterprise"     # + everything, federation, governance
    SOVEREIGN = "sovereign"       # perpetual, all agents, unlimited tokens ($1000)
    INTERNAL = "internal"        # Aitherium dogfood — unrestricted


_TIER_RANK = {
    Tier.COMMUNITY: 0,
    Tier.STARTER: 1,
    Tier.BUILDER: 2,
    Tier.PROFESSIONAL: 3,
    Tier.ENTERPRISE: 4,
    Tier.SOVEREIGN: 5,
    Tier.INTERNAL: 99,
}

# Identities every tier may always run (shipped free in the wheel).
_FREE_AGENTS = frozenset({"aither", "assistant"})


@dataclass
class Entitlements:
    """What a license unlocks.  COMMUNITY defaults are the free tier."""

    named_agents: list[str] = field(default_factory=lambda: list(_FREE_AGENTS))
    max_effort: int = 3          # COMMUNITY: small models only (effort <= 3)
    fleet: bool = False          # multi-agent orchestration
    channels: bool = False       # Discord/Telegram/Slack/Webhook adapters
    auto_neurons: bool = False   # proactive context gathering before LLM
    cron: bool = False           # autonomous scheduled execution
    swarm: bool = False          # Genesis swarm-coding dispatch
    custom_agents: bool = False  # build/load custom identities + packs
    packs: bool = False          # install marketplace packs
    can_use_computer_use: bool = False  # premium AitherPilot browser/computer-use tool pack
    can_use_formbridge: bool = False  # premium FormBridge form automation pack
    can_use_untether: bool = False  # premium UNTETHER CRM + HAR intelligence pack
    monthly_token_limit: int = 100_000  # 0 == unlimited

    @classmethod
    def for_tier(cls, tier: Tier) -> "Entitlements":
        rank = _TIER_RANK[tier]
        if tier is Tier.INTERNAL or tier is Tier.SOVEREIGN:
            return cls(
                named_agents=["*"], max_effort=10, fleet=True, channels=True,
                auto_neurons=True, cron=True, swarm=True, custom_agents=True,
                packs=True, can_use_computer_use=True, can_use_formbridge=True,
                can_use_untether=True, monthly_token_limit=0,
            )
        return cls(
            named_agents=list(_FREE_AGENTS),
            max_effort=3 if rank < 3 else 10,        # reasoning unlocks at PROFESSIONAL
            fleet=rank >= 1,                          # STARTER+
            channels=rank >= 1,                       # STARTER+
            auto_neurons=rank >= 1,                   # STARTER+
            cron=rank >= 2,                           # BUILDER+
            swarm=rank >= 3,                          # PROFESSIONAL+
            custom_agents=rank >= 3,                  # PROFESSIONAL+
            packs=rank >= 2,                          # BUILDER+
            # AitherPilot, FormBridge, and UNTETHER are high-value marquee capabilities
            # delivered AS custom packs, so they track the same tier as custom_agents/swarm
            # (PROFESSIONAL+). Bump this rank to change the monetization line.
            can_use_computer_use=rank >= 3,           # PROFESSIONAL+
            can_use_formbridge=rank >= 3,             # PROFESSIONAL+
            can_use_untether=rank >= 3,               # PROFESSIONAL+
            monthly_token_limit=0 if rank >= 1 else 100_000,
        )


@dataclass
class License:
    """A resolved license."""

    tier: Tier = Tier.COMMUNITY
    entitlements: Entitlements = field(default_factory=Entitlements)
    tenant_id: str = ""
    packs: list[str] = field(default_factory=list)  # explicit pack SKUs owned
    issued_at: float = 0.0
    expires_at: float = 0.0  # 0 == perpetual
    source: str = "default"  # default|env|file|internal|unverified

    @property
    def is_expired(self) -> bool:
        if self.tier is Tier.SOVEREIGN:
            return False
        return bool(self.expires_at) and time.time() > self.expires_at


def _public_key_is_placeholder() -> bool:
    """True when no real verification key is configured (ships fail-closed).

    Until the real Ed25519 public key is baked into ``_LICENSE_PUBLIC_KEY_HEX``
    or supplied via ``AITHER_LICENSE_PUBLIC_KEY``, every signed license fails
    verification and resolves to COMMUNITY. This helper lets callers emit a
    *specific* warning ("no key configured") vs a generic "bad signature".
    """
    pub_hex = os.environ.get("AITHER_LICENSE_PUBLIC_KEY", _LICENSE_PUBLIC_KEY_HEX)
    return (not pub_hex) or set(pub_hex) <= {"0"}


def _verify_signature(payload: bytes, signature_hex: str) -> bool:
    """Verify an Ed25519 signature over *payload*.

    Returns False (reject -> fail-closed) on any error, missing crypto lib,
    placeholder key, or bad signature.
    """
    pub_hex = os.environ.get("AITHER_LICENSE_PUBLIC_KEY", _LICENSE_PUBLIC_KEY_HEX)
    if not pub_hex or set(pub_hex) <= {"0"}:
        # Placeholder/empty key -> cannot verify anything -> reject.
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        try:
            pub.verify(bytes.fromhex(signature_hex), payload)
            return True
        except InvalidSignature:
            return False
    except ImportError:
        logger.debug(
            "cryptography not installed — cannot verify licenses. "
            "Install with: pip install 'awdk[federation]'",
        )
        return False
    except Exception as exc:  # malformed key/signature
        logger.debug("License signature verification error: %s", exc)
        return False


def _license_from_envelope(envelope: dict[str, Any], source: str) -> License | None:
    """Build a verified License from a signed ``{payload, signature}`` envelope.

    The envelope is ``{"payload": <b64 json>, "signature": <hex ed25519>}``.
    Returns None (caller falls back to COMMUNITY) when verification fails.
    """
    try:
        payload_b64 = envelope["payload"]
        signature = envelope["signature"]
    except (KeyError, TypeError):
        logger.warning("License envelope missing payload/signature — ignoring.")
        return None

    payload_bytes = base64.b64decode(payload_b64)
    if not _verify_signature(payload_bytes, signature):
        if _public_key_is_placeholder():
            logger.warning(
                "License present (source=%s) but NO verification key is configured — "
                "resolving to free tier. Set AITHER_LICENSE_PUBLIC_KEY=<hex> (or bake the "
                "real key into adk/licensing.py) so portal-signed licenses verify.",
                source,
            )
        else:
            logger.warning(
                "License signature INVALID (source=%s) — falling back to free tier.",
                source,
            )
        return None

    data = json.loads(payload_bytes.decode("utf-8"))
    try:
        tier = Tier(str(data.get("tier", "community")).lower())
    except ValueError:
        tier = Tier.COMMUNITY

    ent = Entitlements.for_tier(tier)
    # A signed license may narrow/extend specific entitlements explicitly.
    raw_ent = data.get("entitlements") or {}
    for key in (
        "named_agents", "max_effort", "fleet", "channels", "auto_neurons",
        "cron", "swarm", "custom_agents", "packs", "can_use_computer_use",
        "can_use_formbridge", "can_use_untether", "monthly_token_limit",
    ):
        if key in raw_ent:
            setattr(ent, key, raw_ent[key])

    lic = License(
        tier=tier,
        entitlements=ent,
        tenant_id=str(data.get("tenant_id", "")),
        packs=list(data.get("packs", [])),
        issued_at=float(data.get("issued_at", 0) or 0),
        expires_at=float(data.get("expires_at", 0) or 0),
        source=source,
    )
    if lic.is_expired:
        logger.warning("License EXPIRED (source=%s) — falling back to free tier.", source)
        return None
    return lic


def _resolve_license() -> License:
    """Resolve the active license per the documented order. Fail-closed."""
    # Internal dogfood — unrestricted.
    if os.environ.get("AITHER_TENANT_SLUG", "").lower() == "aitherium":
        return License(
            tier=Tier.INTERNAL,
            entitlements=Entitlements.for_tier(Tier.INTERNAL),
            tenant_id="aitherium",
            source="internal",
        )

    # 1) Env-provided signed envelope (base64 of the {payload,signature} JSON).
    env_key = os.environ.get("AITHER_LICENSE_KEY", "").strip()
    if env_key:
        try:
            envelope = json.loads(base64.b64decode(env_key).decode("utf-8"))
            lic = _license_from_envelope(envelope, source="env")
            if lic:
                return lic
        except Exception as exc:
            logger.warning("AITHER_LICENSE_KEY unparseable (%s) — free tier.", exc)

    # 2) ~/.aither/license.json
    path = Path(
        os.environ.get("AITHER_LICENSE_FILE")
        or (Path.home() / ".aither" / "license.json")
    )
    try:
        if path.is_file():
            envelope = json.loads(path.read_text(encoding="utf-8"))
            lic = _license_from_envelope(envelope, source="file")
            if lic:
                return lic
    except Exception as exc:
        logger.warning("Could not read license file %s (%s) — free tier.", path, exc)

    # 3) Default: free tier.
    return License(
        tier=Tier.COMMUNITY,
        entitlements=Entitlements.for_tier(Tier.COMMUNITY),
        source="default",
    )


class LicenseManager:
    """Resolves and answers entitlement questions for the local runtime."""

    def __init__(self, license: License | None = None) -> None:
        self.license = license or _resolve_license()

    # -- reload ----------------------------------------------------------
    def reload(self) -> None:
        """Re-resolve the license (e.g. after a pack purchase syncs a new file)."""
        self.license = _resolve_license()

    # -- enforcement helpers --------------------------------------------
    @staticmethod
    def _enforced() -> bool:
        """Whether gates raise.  Disabled only by explicit opt-out."""
        return os.environ.get("AITHER_LICENSE_ENFORCE", "1").lower() not in (
            "0", "false", "no",
        )

    def require(self, capability: str, *, friendly: str | None = None) -> None:
        """Raise :class:`LicenseError` if *capability* is not entitled.

        ``capability`` is the name of a ``can_use_*``/boolean entitlement
        (e.g. ``"fleet"``, ``"channels"``, ``"cron"``, ``"swarm"``,
        ``"auto_neurons"``, ``"custom_agents"``, ``"packs"``).
        """
        if not self._enforced():
            return
        allowed = bool(getattr(self.license.entitlements, capability, False))
        if not allowed:
            label = friendly or capability.replace("_", " ")
            raise LicenseError(
                f"'{label}' requires a paid tier (current: "
                f"{self.license.tier.value}). Upgrade at {PORTAL_PACKS_URL}"
            )

    # -- queries (used by agent.py and the gated modules) ---------------
    def is_agent_licensed(self, name: str) -> bool:
        ent = self.license.entitlements
        if "*" in ent.named_agents:
            return True
        if name in _FREE_AGENTS or name in ent.named_agents:
            return True
        # Identities delivered via an owned/installed pack are allowed.
        return self._agent_pack_installed(name)

    def can_build_custom_agents(self) -> bool:
        return bool(self.license.entitlements.custom_agents)

    def can_use_fleet(self) -> bool:
        return bool(self.license.entitlements.fleet)

    def can_use_channels(self) -> bool:
        return bool(self.license.entitlements.channels)

    def can_use_auto_neurons(self) -> bool:
        return bool(self.license.entitlements.auto_neurons)

    def can_use_cron(self) -> bool:
        return bool(self.license.entitlements.cron)

    def can_use_swarm(self) -> bool:
        return bool(self.license.entitlements.swarm)

    def can_install_packs(self) -> bool:
        return bool(self.license.entitlements.packs)

    def is_pack_available(self, pack_id: str) -> bool:
        ent = self.license.entitlements
        if "*" in ent.named_agents:  # INTERNAL
            return True
        return pack_id in self.license.packs

    def max_effort(self) -> int:
        if not self._enforced():
            return 10
        return int(self.license.entitlements.max_effort)

    def clamp_effort(self, effort: int | None) -> tuple[int, bool]:
        """Clamp *effort* to the licensed maximum. Returns (effort, was_capped)."""
        if effort is None:
            return effort, False  # let downstream default it
        cap = self.max_effort()
        if effort > cap:
            return cap, True
        return effort, False

    # -- internal --------------------------------------------------------
    @staticmethod
    def _agent_pack_installed(name: str) -> bool:
        """True if an identity YAML for *name* exists in a local pack dir."""
        for base in (
            Path.home() / ".aitheros" / "packs",
            Path.home() / ".aither" / "packs",
        ):
            try:
                if base.is_dir() and any(base.rglob(f"{name}.yaml")):
                    return True
            except Exception:
                continue
        return False


_manager: LicenseManager | None = None


def get_license_manager() -> LicenseManager:
    """Return the process-wide :class:`LicenseManager` singleton."""
    global _manager
    if _manager is None:
        _manager = LicenseManager()
    return _manager


def reset_license_manager() -> None:
    """Drop the cached manager (used by tests after changing env)."""
    global _manager
    _manager = None

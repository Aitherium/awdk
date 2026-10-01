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

Resolution:
    AITHER_LICENSE_ENFORCE=0          -> enforcement disabled (returns allow)
    AITHER_TENANT_SLUG=aitherium      -> INTERNAL
    otherwise every VERIFIED, unexpired license among
      AITHER_LICENSE_KEY=<b64 envelope>
      ~/.aither/license.json            (the ACCOUNT license: `adk login` / `adk license sync`)
      ~/.aither/licenses/*.json         (OFFLINE licenses: pasted keys, one file each)
    counts: the highest tier wins, and the packs are the UNION of them all, so
    an account sync never switches off a pack bought offline and vice versa.
    (nothing verifies)                -> COMMUNITY
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

# Ed25519 public key (hex, 32 bytes) of the CURRENT license signing root: the one
# the shop and the portal sign with today. The matching PRIVATE key lives only in
# the Aitherium platform vault (AITHERIUM_LICENSE_ED25519_SK) and is NEVER shipped.
_LICENSE_PUBLIC_KEY_HEX = (
    "71f4e4da93095b3f1d29a3f01d27358d1398de9f0d5c0d8e3e0cb35199ac6980"
)

# Earlier signing roots, still TRUSTED for verification so that every license
# issued under them keeps working. Rotation is additive: a root moves here when a
# new one replaces it for signing, and is never removed (a pinned test enforces it).
# 1a468a33...: the first root (2026); its private half was lost, nothing new is signed.
_LEGACY_LICENSE_PUBLIC_KEYS_HEX: tuple[str, ...] = (
    "1a468a332d6cfc5378edf7083b6d845bcfdf141fbce28e6dbe521dd6b84e233f",
)

#: Every root a license may be signed by when AITHER_LICENSE_PUBLIC_KEY is not set.
TRUSTED_LICENSE_PUBLIC_KEYS_HEX: tuple[str, ...] = (
    _LICENSE_PUBLIC_KEY_HEX, *_LEGACY_LICENSE_PUBLIC_KEYS_HEX,
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


def trusted_public_keys() -> list[str]:
    """The Ed25519 roots (hex) a license signature is checked against.

    ``AITHER_LICENSE_PUBLIC_KEY`` (one hex key, or several separated by commas or
    whitespace) REPLACES the baked roots, for self-hosted/sovereign signing roots
    and tests. Unset or blank -> :data:`TRUSTED_LICENSE_PUBLIC_KEYS_HEX` (current
    root first, then every legacy root). All-zero placeholder keys are dropped, so
    a placeholder-only configuration verifies nothing (fail-closed).
    """
    raw = os.environ.get("AITHER_LICENSE_PUBLIC_KEY", "")
    keys = raw.replace(",", " ").split() if raw.strip() else list(
        TRUSTED_LICENSE_PUBLIC_KEYS_HEX)
    return [k.strip().lower() for k in keys if k.strip() and not set(k.strip()) <= {"0"}]


def _public_key_is_placeholder() -> bool:
    """True when no real verification key is configured (ships fail-closed).

    When the only configured key is the all-zero placeholder, every signed license
    fails verification and resolves to COMMUNITY. This helper lets callers emit a
    *specific* warning ("no key configured") vs a generic "bad signature".
    """
    return not trusted_public_keys()


def _verify_signature(payload: bytes, signature_hex: str) -> bool:
    """Verify an Ed25519 signature over *payload* against ANY trusted root.

    Returns False (reject -> fail-closed) on any error, missing crypto lib,
    placeholder key, or a signature no trusted root produced.
    """
    keys = trusted_public_keys()
    if not keys:
        # Placeholder/empty key -> cannot verify anything -> reject.
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        logger.debug(
            "cryptography not installed — cannot verify licenses. "
            "Install with: pip install 'awdk[federation]'",
        )
        return False
    try:
        signature = bytes.fromhex(signature_hex)
    except (TypeError, ValueError) as exc:
        logger.debug("License signature is not hex: %s", exc)
        return False
    for pub_hex in keys:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
            pub.verify(signature, payload)
            return True
        except InvalidSignature:
            continue
        except Exception as exc:  # malformed key -> try the next root
            logger.debug("License verification key unusable: %s", exc)
            continue
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


def license_file_path() -> Path:
    """The ACCOUNT license (``AITHER_LICENSE_FILE`` overrides ``~/.aither/license.json``).

    Written only by an account sign-in / sync. Offline keys go to :func:`licenses_dir`.
    """
    return Path(
        os.environ.get("AITHER_LICENSE_FILE")
        or (Path.home() / ".aither" / "license.json")
    ).expanduser()


def licenses_dir() -> Path:
    """OFFLINE licenses, one ``<fingerprint>.json`` per key, beside the account file.

    ``AITHER_LICENSES_DIR`` overrides. Every verified file here is unioned into the
    active license, so installing one never replaces another.
    """
    raw = os.environ.get("AITHER_LICENSES_DIR", "").strip()
    return Path(raw).expanduser() if raw else license_file_path().parent / "licenses"


def envelope_fingerprint(envelope: dict[str, Any]) -> str:
    """Stable file name for an envelope (same scheme Saga used for its copies)."""
    import hashlib

    return hashlib.sha256(str(envelope.get("payload", "")).encode("utf-8")).hexdigest()[:16]


def parse_license_text(text: str) -> dict[str, Any]:
    """Normalise any license shape Aitherium hands out to the inner envelope.

    Accepts the ``{payload, signature}`` JSON, a ``{"license": ...}`` wrapper, the
    base64 outer key, or an ``AITHER_LICENSE_KEY=<key>`` line. Raises ValueError.
    """
    t = (text or "").strip().lstrip("﻿")
    if not t:
        raise ValueError("empty license")
    if t.upper().startswith("AITHER_LICENSE_KEY="):
        t = t.split("=", 1)[1].strip()
    t = t.strip().strip('"').strip("'")
    try:
        if t.startswith("{"):
            env = json.loads(t)
        else:
            env = json.loads(base64.b64decode("".join(t.split()), validate=True).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- every decode failure is the same answer
        raise ValueError("not an Aitherium license") from exc
    if isinstance(env, dict) and isinstance(env.get("license"), dict):
        env = env["license"]
    if not (isinstance(env, dict) and isinstance(env.get("payload"), str)
            and isinstance(env.get("signature"), str)):
        raise ValueError("license is missing its payload or signature")
    return {"payload": env["payload"], "signature": env["signature"]}


def install_offline_license(envelope: dict[str, Any]) -> tuple[License, Path]:
    """Verify *envelope* and keep it under :func:`licenses_dir`. Never touches others.

    Raises ValueError when it does not verify (nothing is written). Returns the
    verified License and the file it was saved to.
    """
    lic = _license_from_envelope(envelope, source="offline")
    if lic is None:
        raise ValueError("license signature did not verify (or it has expired)")
    dest = licenses_dir() / f"{envelope_fingerprint(envelope)}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps({"payload": envelope["payload"],
                               "signature": envelope["signature"]}, indent=2),
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, dest)
    reset_license_manager()
    return lic, dest


def _candidate_licenses() -> list[License]:
    """Every verified, unexpired license this runtime can see (env, account, offline)."""
    found: list[License] = []

    env_key = os.environ.get("AITHER_LICENSE_KEY", "").strip()
    if env_key:
        try:
            envelope = json.loads(base64.b64decode(env_key).decode("utf-8"))
            lic = _license_from_envelope(envelope, source="env")
            if lic:
                found.append(lic)
        except Exception as exc:
            logger.warning("AITHER_LICENSE_KEY unparseable (%s) — free tier.", exc)

    path = license_file_path()
    try:
        if path.is_file():
            envelope = json.loads(path.read_text(encoding="utf-8"))
            lic = _license_from_envelope(envelope, source="file")
            if lic:
                found.append(lic)
    except Exception as exc:
        logger.warning("Could not read license file %s (%s) — free tier.", path, exc)

    try:
        ldir = licenses_dir()
        offline = sorted(ldir.glob("*.json")) if ldir.is_dir() else []
    except OSError:
        offline = []
    for extra in offline:
        try:
            envelope = json.loads(extra.read_text(encoding="utf-8"))
            lic = _license_from_envelope(envelope, source="offline")
        except Exception as exc:  # noqa: BLE001 -- one bad file never hides the rest
            logger.debug("offline license %s skipped (%s)", extra, exc)
            continue
        if lic:
            found.append(lic)
    return found


def _resolve_license() -> License:
    """Resolve the active license. Fail-closed; union of packs across sources."""
    # Internal dogfood — unrestricted.
    if os.environ.get("AITHER_TENANT_SLUG", "").lower() == "aitherium":
        return License(
            tier=Tier.INTERNAL,
            entitlements=Entitlements.for_tier(Tier.INTERNAL),
            tenant_id="aitherium",
            source="internal",
        )

    found = _candidate_licenses()
    if not found:
        return License(
            tier=Tier.COMMUNITY,
            entitlements=Entitlements.for_tier(Tier.COMMUNITY),
            source="default",
        )
    # Highest tier wins (earliest source on a tie: env, then account, then offline);
    # packs are the union, so no source can switch off a pack another one grants.
    best = max(range(len(found)), key=lambda i: (_TIER_RANK[found[i].tier], -i))
    primary = found[best]
    packs: list[str] = []
    for lic in [primary, *found]:
        for pack in lic.packs:
            if pack not in packs:
                packs.append(pack)
    primary.packs = packs
    return primary


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


    # -- AitherGrid -------------------------------------------------------
    def grid_plan(self) -> "GridPlan":
        """The AitherGrid tier this license grants (Starter when it grants none).

        A Grid purchase arrives as its SKU id in the signed payload's ``packs``
        list (the minter's ``packs=`` field). The best owned tier wins.
        Enforcement off, INTERNAL and SOVEREIGN resolve to Enterprise.
        """
        if not self._enforced() or self.license.tier in (Tier.INTERNAL, Tier.SOVEREIGN):
            return GRID_PLANS[GRID_ENTERPRISE_SKU]
        best = GRID_PLANS[GRID_STARTER_SKU]
        for sku in self.license.packs:
            canonical = GRID_SKU_ALIASES.get(str(sku), str(sku))
            plan = GRID_PLANS.get(canonical)
            if plan is not None and plan.rank > best.rank:
                best = plan
        return best

    def grid_node_limit(self) -> int:
        """Max grid nodes (the local node counts as one); -1 == unlimited."""
        return self.grid_plan().max_nodes

    def has_grid_feature(self, feature: str) -> bool:
        return feature in self.grid_plan().features

    def require_grid_feature(self, feature: str) -> None:
        """Raise :class:`LicenseError` unless the Grid tier includes *feature*."""
        plan = self.grid_plan()
        if feature not in plan.features:
            raise LicenseError(
                f"Grid feature '{feature}' is not in {plan.name}. "
                f"Upgrade: adk upgrade grid-pro (or grid-enterprise)"
            )


# ── AitherGrid tiers ────────────────────────────────────────────────────────
# Mirrors the platform's Grid SKUs (node limits and entitlements). The wheel
# cannot read the platform billing book, so the copy lives here; a platform-side
# test fails when the two drift.
GRID_STARTER_SKU = "grid_starter"
GRID_PRO_SKU = "grid_pro_monthly"
GRID_ENTERPRISE_SKU = "grid_enterprise_monthly"


@dataclass(frozen=True)
class GridPlan:
    sku: str
    name: str
    rank: int
    max_nodes: int  # -1 == unlimited
    features: frozenset


_GRID_STARTER_FEATURES = frozenset({"grid", "effort_routing", "gpu_orchestrator"})
_GRID_PRO_FEATURES = _GRID_STARTER_FEATURES | {
    "auto_failover", "health_dashboard", "alerts", "priority_cloud", "cloud_sync",
}
_GRID_ENTERPRISE_FEATURES = _GRID_PRO_FEATURES | {
    "fleet_management", "multi_tenant", "mtls", "metering", "wan_support",
}

GRID_PLANS: dict[str, GridPlan] = {
    GRID_STARTER_SKU: GridPlan(GRID_STARTER_SKU, "Grid Starter", 0, 2, _GRID_STARTER_FEATURES),
    GRID_PRO_SKU: GridPlan(GRID_PRO_SKU, "Grid Pro", 1, 10, _GRID_PRO_FEATURES),
    GRID_ENTERPRISE_SKU: GridPlan(
        GRID_ENTERPRISE_SKU, "Grid Enterprise", 2, -1, _GRID_ENTERPRISE_FEATURES),
}

# Storefront listings sold as the same product (billing-book `alias_of`).
GRID_SKU_ALIASES: dict[str, str] = {
    "service.grid": GRID_PRO_SKU,
    "infra.grid-distributed": GRID_ENTERPRISE_SKU,
}


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

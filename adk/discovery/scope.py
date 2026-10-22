"""The scope filter every discovery card passes before it is written or shown.

It applies the layers of the scope contract that can be judged on this machine:

* caller class -- a gateway card (tool, agent, catalog pack) is shown to a non-platform
  caller ONLY when it was fetched with THAT caller's bearer (``fetched_for`` equals the
  caller's fingerprint). The gateway's per-bearer entitlement filter is the scope
  judgement; a list fetched for anyone else is never re-used for this caller, so a
  card with no mark is dropped (an allowlist, not a denylist). Admin tool families
  (secrets, minting, provisioning, k3s, ring, fleet, DGX, Hetzner, Cloudflare,
  runners, ...) are then dropped for every non-platform caller as a second pass;
* plan -- a catalog pack whose tier is above the caller's tier is dropped;
* licence -- a toolpack whose entitlement check fails is dropped;
* health -- tools known to answer ``not_implemented`` / 501 are dropped.

Fail closed: when the caller cannot be identified it is treated as a free-tier,
non-platform caller with no fingerprint (every gateway card is dropped). An unknown
CARD tier ranks above every known one (hidden); an unknown CALLER tier ranks LOWEST
(``community``). The two orderings are opposite on purpose: neither unknown grants
anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Lowest to highest. Matches ``adk.tool_pack_loader._TIER_ORDER`` plus ``platform``.
TIER_ORDER = ("community", "free", "builder", "professional", "enterprise",
              "internal", "platform")

#: Tool families only a platform caller may see (scope contract layer 7). This is the
#: SECOND pass: the first is ``fetched_for`` (see :func:`admit`), so a family missing
#: here is still hidden from any caller whose own gateway list does not carry it.
ADMIN_PREFIXES = (
    "fleet_", "activate_", "deactivate_", "stripe_admin_", "dev_", "federation_",
    "get_secret", "set_secret", "delete_secret", "list_secrets", "secretguard_",
    "mint_", "provision_", "k3s_", "ring_", "hetzner_", "runners_", "restart_dgx_",
    "dgx_", "get_dgx_", "cloudflare_", "cf_", "deploy_remote_", "redundancy_",
    "stack_reconcile_", "restart_local_orchestrator", "watch_restart_component",
    "compute_register_", "compute_remove_", "compute_scale", "reasoning_set_",
    "reasoning_switch_", "reasoning_approve_", "set_inference_endpoint",
    "configure_inference", "lambda_", "onboard_", "set_cloud_provider_key",
    "remove_cloud_provider_key", "list_cloud_provider_keys", "workspace_cycle_",
    "storage_report_apply", "storage_manage_order", "export_to_aithershell",
)

#: Card sources whose list the gateway filtered by the bearer that fetched it.
CALLER_SCOPED_SOURCES = frozenset({"gateway", "gateway-agent", "catalog"})

#: Tools whose handler is a stub or 501 today (scope contract layer 8).
BROKEN_PREFIXES = ("federation_",)
BROKEN_NAMES = frozenset({
    "workspace_directory_create_role", "workspace_directory_list_groups",
    "workspace_directory_create_group", "workspace_directory_update_group",
    "workspace_mail_connect", "workspace_mail_disconnect", "workspace_mail_list_accounts",
    "workspace_mail_list_messages", "workspace_mail_read_message",
    "workspace_mail_mark_read", "workspace_mail_folders", "workspace_mail_search",
    "workspace_mail_send", "workspace_mail_sync",
})

#: Tool name prefixes whose handler calls a third-party cloud API. Such a card is
#: marked ``hosting: hosted`` so "runs on my box" is never assumed for it.
THIRD_PARTY_PREFIXES = ("rf_", "roboflow_", "cf_", "cloudflare_", "stripe_", "hetzner_",
                        "vast", "lambda_", "aither_gpu_", "search_gpu_marketplace",
                        "launch_gpu_instance", "deploy_cloud_", "teardown_cloud_",
                        "web_search", "dr_web_search", "commerce_", "send_email",
                        "send_user_email", "smtp_")

#: Licences that need a human decision before a card is used in a product.
COPYLEFT_MARKERS = ("agpl", "gpl", "sspl", "busl", "bsl", "commons clause",
                    "non-commercial", "noncommercial", "enterprise")


@dataclass(frozen=True)
class Caller:
    """Who the index is built for. ``known`` False means every check fails closed."""

    tenant_id: str = ""
    tier: str = "free"
    roles: tuple = field(default_factory=tuple)
    known: bool = False
    #: sha256 of the bearer, 12 hex (``sources.bearer_fingerprint``); "" = no bearer.
    fingerprint: str = ""
    #: the tier string the gateway sent when TIER_ORDER does not know it.
    tier_raw: str = ""

    @property
    def platform(self) -> bool:
        return self.known and self.tenant_id == "platform" and self.tier == "platform"

    @property
    def tier_error(self) -> str:
        if not self.tier_raw:
            return ""
        return f"unknown caller tier {self.tier_raw!r} ranked as {TIER_ORDER[0]}"

    def as_dict(self) -> dict:
        return {"tenant_id": self.tenant_id, "tier": self.tier, "roles": list(self.roles),
                "known": self.known, "platform": self.platform,
                "fingerprint": self.fingerprint, "tier_raw": self.tier_raw}


def caller_from_me(doc: Any, fingerprint: str = "") -> Caller:
    """A Caller from the gateway's ``/v1/auth/me`` body, or from a stored ``as_dict``.

    Anything odd is unknown. ``fingerprint`` is the bearer the body was fetched with;
    a stored dict carries its own.
    """
    fp = fingerprint or (str(doc.get("fingerprint") or "") if isinstance(doc, dict) else "")
    if not isinstance(doc, dict) or not doc.get("tenant_id"):
        return Caller(fingerprint=fp)
    roles = doc.get("roles") if isinstance(doc.get("roles"), list) else []
    raw = str(doc.get("tier") or "free").strip().lower()
    if raw in TIER_ORDER:
        tier, tier_raw = raw, str(doc.get("tier_raw") or "")
    else:
        tier, tier_raw = TIER_ORDER[0], raw
    return Caller(tenant_id=str(doc["tenant_id"]), tier=tier,
                  roles=tuple(str(r) for r in roles), known=True, fingerprint=fp,
                  tier_raw=tier_raw)


def tier_rank(tier: str) -> int:
    """A CARD's tier. Unknown ranks above every known one, so the card is hidden."""
    t = (tier or "").strip().lower()
    return TIER_ORDER.index(t) if t in TIER_ORDER else len(TIER_ORDER)


def caller_rank(tier: str) -> int:
    """A CALLER's tier. Unknown ranks LOWEST, so it is granted nothing extra."""
    t = (tier or "").strip().lower()
    return TIER_ORDER.index(t) if t in TIER_ORDER else 0


def license_flag(text: str) -> str:
    """``""`` for a permissive or unstated licence, else the marker that matched."""
    low = (text or "").lower()
    for marker in COPYLEFT_MARKERS:
        if marker in low:
            return marker
    return ""


def tool_hosting(name: str) -> str:
    return "hosted" if name.startswith(THIRD_PARTY_PREFIXES) else "fleet"


def admit(card: dict, caller: Caller) -> "tuple[bool, str]":
    """(shown, reason-if-not). Every card goes through here."""
    name = str(card.get("name") or "")
    domain = card.get("domain")
    if not name or domain not in ("tools", "skills", "packs", "agents"):
        return False, "malformed card"
    if card.get("hosting") not in ("local", "fleet", "hosted"):
        return False, "no hosting mark"
    if domain == "tools" and card.get("source") == "gateway":
        if name in BROKEN_NAMES or name.startswith(BROKEN_PREFIXES):
            return False, "handler not implemented"
        if name.startswith(ADMIN_PREFIXES) and not caller.platform:
            return False, "platform-only"
    if card.get("source") in CALLER_SCOPED_SOURCES and not caller.platform:
        fetched_for = str(card.get("fetched_for") or "")
        if not fetched_for or not caller.fingerprint or fetched_for != caller.fingerprint:
            return False, "fetched for another caller"
    if card.get("min_tier") and not caller.platform:
        if tier_rank(str(card["min_tier"])) > caller_rank(caller.tier):
            return False, "above caller tier"
    if card.get("entitled") is False:
        return False, "not entitled"
    return True, ""


def filter_cards(cards: list, caller: Caller) -> "tuple[list, dict]":
    """(admitted cards, {reason: count dropped})."""
    kept: list = []
    dropped: dict = {}
    for card in cards:
        ok, why = admit(card, caller)
        if ok:
            kept.append(card)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    return kept, dropped


__all__ = ["ADMIN_PREFIXES", "BROKEN_NAMES", "CALLER_SCOPED_SOURCES", "Caller", "admit",
           "caller_from_me", "caller_rank", "filter_cards", "license_flag", "tier_rank",
           "tool_hosting"]

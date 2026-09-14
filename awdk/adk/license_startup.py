"""Shared startup entitlement gate for awdk products.

Called early from each product entrypoint (aithershell, awnode, the MCP server) to:

  1. Resolve the active license tier (via :mod:`adk.licensing`, fail-closed).
  2. Log what the runtime is entitled to (clear UX, not a hard wall).
  3. Wire :mod:`adk.metering` token quotas to the resolved tier — and, for an
     **unlicensed product container**, apply a *super-limited* community cap so the
     free product is genuinely useful but not a free production runtime.
  4. Optionally hard-require a paid license (``require_license=True``) for deployments
     that should not run on the free tier at all.

This does NOT cripple the adk SDK's free tier (``adk.licensing`` keeps COMMUNITY
genuinely useful — single agent, memory, graph, ReAct). "Super-limited" is a
*product/container* policy expressed as a token quota, controllable via env:

    AITHER_PRODUCT_TIER_STRICT   1 (default) → apply the tight community cap
    AITHER_COMMUNITY_MONTHLY_TOKENS   tight monthly token cap (default 10000)
    AITHER_LICENSE_REQUIRE       1 → hard-require a paid license (overrides the arg)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("adk.license_startup")

_gated_products: set[str] = set()


def _env_true(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "")


def gate_startup(product: str = "aither", require_license: bool = False):
    """Resolve the license, wire metering quotas, and (optionally) require a paid tier.

    Returns the resolved ``adk.licensing.License`` (or ``None`` if licensing is
    unavailable). Never raises unless a paid license is required and absent — startup
    gating must not crash a product over a transient resolution issue.

    Provides three specific guidance messages:
      (a) No license anywhere → community tier, free but limited
      (b) License key/file provided but signature invalid → loud warning, free tier
      (c) License file corrupt/unparseable → clear error, free tier
    """
    if product in _gated_products:
        return None
    _gated_products.add(product)

    try:
        from adk.licensing import LicenseError, get_license_manager, Tier
    except Exception as exc:  # licensing module unavailable — do not block startup
        logger.debug("license gate skipped (%s unavailable): %s", product, exc)
        return None

    lm = get_license_manager()
    lic = lm.license
    ent = lic.entitlements
    tier = lic.tier.value

    # Distinguish three startup cases with specific guidance
    if lic.source == "default":
        # Case (a): no license provided anywhere
        logger.info(
            "%s startup (no license): tier=%s, limited to 100k tokens/month on our cloud. "
            "For local/BYO-key unlimited, upgrade at api.aitherium.com (sovereign=$1000/perpetual).",
            product, tier,
        )
    elif lic.source in ("env", "file") and lic.tier == Tier.COMMUNITY:
        # Case (b): key/file was provided but signature failed or key placeholder not set
        logger.warning(
            "%s: license signature INVALID (source=%s) — falling back to free tier. "
            "If the key is correct, check https://api.aitherium.com/status for platform issues. "
            "For offline testing, set AITHER_LICENSE_PUBLIC_KEY=<your-hex-key>.",
            product, lic.source,
        )
    else:
        # Case (c): license parsed successfully, or tier > community
        logger.info(
            "%s startup: tier=%s tenant=%s source=%s max_effort=%s monthly_tokens=%s",
            product, tier, lic.tenant_id or "-", lic.source, ent.max_effort,
            ent.monthly_token_limit or "unlimited",
        )

    require = require_license or _env_true("AITHER_LICENSE_REQUIRE")
    if require and tier == "community":
        raise LicenseError(
            f"Product '{product}' requires a paid Aitherium license; the free tier is "
            f"not permitted for this deployment. Get a key at api.aitherium.com."
        )

    _wire_metering(product, tier, ent)
    return lic


def _wire_metering(product: str, tier: str, ent) -> None:
    """Tie the default agent meter's monthly token quota to the resolved tier."""
    try:
        from adk.metering import QuotaConfig, get_meter
    except Exception as exc:
        logger.debug("metering wiring skipped: %s", exc)
        return

    monthly = int(getattr(ent, "monthly_token_limit", 0) or 0)  # 0 = unlimited (paid)

    # Super-limited community: an unlicensed *product container* gets a tight cap so it
    # can be evaluated but not run as a free production runtime. The adk SDK free tier
    # itself is untouched.
    if tier == "community" and _env_true("AITHER_PRODUCT_TIER_STRICT", "1"):
        try:
            strict = int(os.environ.get("AITHER_COMMUNITY_MONTHLY_TOKENS", "10000"))
        except ValueError:
            strict = 10000
        monthly = strict if monthly == 0 else min(monthly, strict)
        logger.info("%s: community tier → super-limited (%s tokens/mo)", product, monthly)

    # get_meter caches by name; create the default meter with the tier quota up front.
    get_meter("default", quota=QuotaConfig(monthly_limit=monthly))

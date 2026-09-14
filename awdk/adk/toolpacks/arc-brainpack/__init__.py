"""arc-brainpack — crowdsource + earn tool pack for the AitherWorldModel.

Like the other in-repo packs (see arcsteer), this registers its tools into an
agent's ADK ToolRegistry when the pack is activated by name (register_tool_packs
-> tool_pack_loader). The five arc_* tools front the PUBLIC Contribution Gateway
(/v1/*, `Authorization: Bearer <token>`) with a self-contained client plus a
vendored random-policy ARC player (no external checkout required):

  arc_register([handle])        mint/exchange an ACTA wallet -> POST /v1/register
                                -> persist the returned contributor token locally
  arc_contribute(games[, n])    play real ARC games random-policy, submit transitions
  arc_enroll(game[, episodes])  enroll a real game through env_enroll: play with
                                your OWN policy via the ArcGatewayAdapter, learn it
                                in the local world model, contribute every transition
  arc_status()                  this token's server-side accept count / quarantine
  arc_leaderboard([limit])      GET /v1/leaderboard — top contributors
  arc_solo()                    print the own-stack one-command bootstrap

Free-tier: they register UNCONDITIONALLY (no entitlement gate). Contributing to
the public world model is unmetered; submission is gated SERVER-SIDE by the
gateway Bearer token. Fail-closed: any import error registers nothing and never
raises (a pack failure must not break agent boot).

Loader note: the pack directory is `arc-brainpack` (hyphen), so the dotted
`tool_modules` path is not importable as a module. The ADK loader detects that
and file-loads THIS __init__.py by path with the pack dir on
submodule_search_locations, so `from . import tools` resolves correctly.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("arc-brainpack")

PACK_ID = "arc-brainpack"


def register(registry) -> int:
    """Register the arc_* tools into *registry*. Returns the count registered.
    Fail-closed: any import/registration failure registers as many as it can and
    never raises (a pack failure must not break agent boot)."""
    try:
        from . import tools as T
        fns = (
            T.arc_register,
            T.arc_contribute,
            T.arc_enroll,
            T.arc_status,
            T.arc_leaderboard,
            T.arc_solo,
        )
    except Exception as exc:  # noqa: BLE001 — pack import failure must not break boot
        logger.warning("arc-brainpack pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for fn in fns:
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("arc-brainpack: skip tool %s: %s",
                         getattr(fn, "__name__", "?"), exc)
    logger.info("arc-brainpack registered %d arc_* tools", n)
    return n

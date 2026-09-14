"""GitHub Runners tool pack — register runners_* tools on an adk agent.

Answers "why is CI stuck?" without a human reading `gh api` output.

The pack exists because of a specific, expensive shape: a repo's CI sat queued
for a day while a self-hosted runner was ONLINE and IDLE the whole time. Two
things were true at once and neither was visible from any single view —

  * three of four registrations were GHOSTS (no agent on any host), so the fleet
    looked three times larger than it was; and
  * the workflow asked for `ubuntu-latest`, so no number of self-hosted runners
    could ever have matched it.

The expensive wrong answer is "add more runners". `runners_diagnose_queue`
therefore checks the LABEL MISMATCH first, and `runners_status` cross-references
registrations against install directories on the host so a ghost is visible
rather than counted as capacity.

Read-only by default. The one mutating tool, `runners_delete_ghost`, refuses an
ONLINE runner outright and requires `confirm=True` for anything else — deleting
a live registration removes real capacity, and "offline" is not proof of a ghost
(the host may simply be powered off).

Provisioning is deliberately NOT a tool: it installs a service, needs elevation,
and is long and interactive. `runners_setup_playbook` returns the exact commands
and the four traps instead, which is the honest shape for something an agent
cannot safely half-finish.

Registration is unconditional and every tool fails soft: an unreachable `gh` is a
STATUS saying it could not judge, never a silent "everything is fine".
"""
from __future__ import annotations

import logging

logger = logging.getLogger("github_runners_pack")

_TOOL_NAMES = (
    "runners_status",
    "runners_diagnose_queue",
    "runners_delete_ghost",
    "runners_setup_playbook",
)


def register(registry) -> int:
    """Register every runners_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("github_runners pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("github_runners: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("github_runners: skip tool %s: %s", name, exc)
    logger.info("GitHub Runners pack registered %d runners_* tools", n)
    return n

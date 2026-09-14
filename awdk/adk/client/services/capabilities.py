"""Effective-capability client — what an agent may do for the AUTHENTICATED user.

This is a THIN CLIENT ON PURPOSE. The answer is computed once, on the platform,
by joining the per-agent capability tokens with the caller's RBAC role; adk,
awnode and AitherShell all read that one answer rather than each deriving
their own.

Two hand-maintained answers to "what may this agent do" drift, and the failure is
asymmetric — a surface that OVER-states reach invites a human to authorise
something the fleet then refuses, while one that under-states it looks like a
broken feature. Neither is discoverable from the surface itself.

WHY IT IS HTTP AND NOT AN IMPORT. This package publishes to PyPI, and importing
``lib.security.agent_principal`` would be an unguarded monorepo import — a
``ModuleNotFoundError`` on a stranger's machine (gate 1i, ADK002). It also could
not work: the resolver needs the CapabilityEngine's tokens and the RBAC store,
neither of which exists outside the fleet.

THERE IS NO ``principal`` PARAMETER, DELIBERATELY. The platform takes the
principal from the verified session behind this request. Letting a caller name
one would key an authz decision on caller-supplied input, which is exactly how a
second ceiling becomes a way to claim someone else's.
"""

from typing import Any, Dict, Optional

from adk.client._base import ServiceClient


class CapabilitiesClient(ServiceClient):
    """Client for the platform's effective-capability resolver."""

    async def effective(self, agent_id: str,
                        timeout: float = 15.0) -> Dict[str, Any]:
        """What ``agent_id`` may do on behalf of the authenticated caller.

        Returns the resolver's view unchanged::

            {"available": bool, "agent_id": str, "principal": str|None,
             "capabilities": [str], "bounded_by_principal": bool,
             "resolved": bool}

        ``resolved`` is the field to branch on, not an empty ``capabilities``
        list: "this agent holds no grants" and "the lookup failed" are different
        facts and must not render identically. ``bounded_by_principal`` says
        whether the list was narrowed by the caller's role or is the agent's own
        ceiling.
        """
        return await self._get(f"/internals/capabilities/effective/{agent_id}",
                               timeout=timeout)

    async def may(self, agent_id: str, resource: str,
                  action: str = "execute") -> Optional[bool]:
        """Would ``agent_id`` be allowed ``action`` on ``resource``?

        Returns None when the answer could not be obtained — NOT False. A UI that
        renders an unreachable resolver as "denied" teaches operators that the
        feature is broken; one that renders it as "allowed" is worse. The caller
        must decide what to do with "unknown", which it can only do if it is told.

        This is advisory. The authoritative check happens at the call site inside
        the fleet, and this client cannot and must not substitute for it.
        """
        view = await self.effective(agent_id)
        if not view.get("resolved"):
            return None
        wanted = f"{resource}:{action}"
        caps = view.get("capabilities") or []
        if wanted in caps:
            return True
        # Mirror the platform's subsumption rule: a broader verb implies a
        # narrower one, and `read` never implies `execute`.
        implied = {"read": ("read", "write", "execute")}.get(action, (action,))
        for cap in caps:
            spec, _, granted = cap.partition(":")
            if spec != resource:
                continue
            if granted == "*" or granted in implied:
                return True
        return False

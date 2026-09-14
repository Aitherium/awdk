"""Federated world-model sync — share tool policy, never knowledge.

An agent's world model learns which action tends to move which bounded score.
That is BEHAVIOURAL, and it is the reason a shared model is legitimate between
colleagues who must not share what they know: consultants at one firm serving
different clients can pool "search projects before searching staff" without
either of them learning anything about the other's client.

What crosses the wire is per-action AGGREGATES — a count and eight summed
deltas — never a transition, a document, a query or a filename. The local fit is
``bias[a][i] = sum_delta[a][i] / count[a]``, and both terms are additive, so::

    bias_federated[a][i] = (Σ_agents sum_delta[a][i]) / (Σ_agents count[a])

is EXACTLY the bias one model trained on everyone's pooled transitions would
produce. Sending raw events would buy no accuracy and would put per-event
records on the wire, so this client cannot send them: it has no code to.

Scope is never in the body. The hub derives tenant, workspace and agent from the
authenticated caller and rejects a payload that names any of them, so this
client sends only ``{"stats": ...}``.

Usage::

    from adk.sync.federation import FederationClient

    client = FederationClient(bearer=token)
    client.contribute(world_model, allowed_actions=pack_tools)
    client.adopt(world_model)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("adk.federation")

__all__ = ["FederationClient", "sync_agent"]

#: In-network default. Never plain http to an internal service.
_DEFAULT_BRAIN = "https://aitheros-aitherbrain:8271"

_TIMEOUT = float(os.environ.get("AITHER_FEDERATION_TIMEOUT", "20"))


def _brain_url() -> str:
    """Resolve the hub, preferring explicit configuration.

    Mirrors the resolution ladder the knowledge-sync client uses, so the two
    cannot disagree about where the brain is — a class of defect that has
    already cost this codebase a silently misaddressed sync.
    """
    explicit = os.environ.get("AITHER_BRAIN_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from adk.config import load_saved_config

        saved = (load_saved_config() or {}).get("brain_url", "")
        if saved:
            return str(saved).rstrip("/")
    except ImportError:
        # Expected when the SDK is embedded without its config module.
        logger.debug("adk.config unavailable — using the in-network brain default")
    except Exception as exc:
        # NOT expected: config exists and could not be read. Distinct from the
        # above, because "no config" and "unreadable config" want different
        # fixes and a bare `pass` makes them the same event.
        logger.debug("could not read saved brain_url (%s) — using the default", exc)
    return _DEFAULT_BRAIN


class FederationClient:
    """Push this agent's aggregates to its workspace model, and adopt the merge.

    Every method fails SOFT and says so in its return value. This is background
    telemetry riding alongside real work: a hub that is down, unreachable or
    rejecting must never break the turn the agent is in the middle of. It must
    also never look like success — each method returns a dict carrying ``ok``,
    so a caller can log a drop rather than assume one did not happen.
    """

    def __init__(
        self,
        bearer: Optional[str] = None,
        brain_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.brain_url = (brain_url or _brain_url()).rstrip("/")
        self.bearer = bearer or os.environ.get("AITHER_SESSION_BEARER", "").strip()
        self.timeout = timeout or _TIMEOUT

    # ---------------------------------------------------------------- internals

    def _headers(self) -> dict:
        """The caller's own credential.

        Emit no header rather than an empty one: an empty ``Authorization``
        reads as a malformed request, while its absence reads as what it is.
        """
        return {"Authorization": self.bearer} if self.bearer else {}

    def _request(self, method: str, path: str, payload: Any = None) -> dict:
        try:
            import httpx
        except ImportError:
            return {"ok": False, "error": "httpx unavailable"}

        if not self.bearer:
            # Fail closed locally. An unauthenticated federation call cannot
            # succeed, and sending it anyway only produces a confusing 403 in
            # the hub's log attributed to nobody.
            return {"ok": False, "error": "no credential — not sending"}

        url = f"{self.brain_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method, url, headers=self._headers(), json=payload,
                )
        except Exception as exc:
            logger.debug("federation %s %s failed: %s", method, path, exc)
            return {"ok": False, "error": str(exc)[:200]}

        if resp.status_code >= 400:
            detail = (resp.text or "")[:200]
            logger.debug("federation %s %s -> %s %s",
                         method, path, resp.status_code, detail)
            return {"ok": False, "status": resp.status_code, "error": detail}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "error": "hub returned non-JSON"}
        data["ok"] = True
        return data

    # ------------------------------------------------------------------- public

    def contribute(self, world_model: Any, allowed_actions: Any = None) -> dict:
        """Send this agent's per-action totals.

        The whitelist is passed straight through to the model's exporter, which
        fails closed without one. This client deliberately does not default it:
        the set of shareable actions is the caller's pack, and inventing one
        here would be inventing a disclosure policy.
        """
        export = getattr(world_model, "export_federation_stats", None)
        if not callable(export):
            return {"ok": False, "error": "world model cannot export aggregates"}

        stats = export(allowed_actions=allowed_actions) or {}
        if not stats:
            # Not an error. A cold agent, or one whose tools are all outside the
            # whitelist, has nothing to say yet.
            return {"ok": True, "skipped": "no shareable aggregates", "sent_actions": 0}

        out = self._request("POST", "/brain/federation/contribute", {"stats": stats})
        out.setdefault("sent_actions", len(stats))
        return out

    def adopt(self, world_model: Any) -> dict:
        """Pull the workspace bias and apply it where local evidence is thin.

        The model itself decides what to keep — an action this agent has real
        experience of keeps its own bias. See ``apply_federated_bias``.
        """
        out = self._request("GET", "/brain/federation/model")
        if not out.get("ok"):
            return out

        apply_fn = getattr(world_model, "apply_federated_bias", None)
        if not callable(apply_fn):
            return {"ok": False, "error": "world model cannot adopt a bias"}

        adopted = apply_fn(out.get("bias") or {})
        return {
            "ok": True,
            "adopted_actions": adopted,
            "offered_actions": out.get("actions", 0),
            "contributors": out.get("contributors", 0),
        }

    def sync(self, world_model: Any, allowed_actions: Any = None) -> dict:
        """One full cycle: contribute, then adopt.

        In that order deliberately. Contributing first means this agent's own
        observations are already in the merge it is about to read, so a lone
        agent in a fresh workspace gets its own bias back rather than an empty
        model — federation degrades to "no change" for the first employee rather
        than to "nothing works".

        Never raises. A failed leg is reported, not thrown: this runs beside real
        work on a schedule, and a hub outage must cost an agent nothing.
        """
        pushed = self.contribute(world_model, allowed_actions=allowed_actions)
        pulled = self.adopt(world_model)
        return {
            "ok": bool(pushed.get("ok")) and bool(pulled.get("ok")),
            "contributed": pushed,
            "adopted": pulled,
        }

    def status(self) -> dict:
        return self._request("GET", "/brain/federation/status")

    def withdraw(self) -> dict:
        """Take this agent's weight back out of the shared model."""
        return self._request("DELETE", "/brain/federation/contribution")


def sync_agent(agent_name: str, allowed_actions: Any = None,
               bearer: Optional[str] = None) -> dict:
    """Federate one named agent's world model. The entry point a driver calls.

    Kept here rather than in the CLI so the cadence is a deployment choice — a
    cron job, a routine, or an agent-loop hook can all drive it without this
    module caring which. It loads the agent's own checkpoint through the public
    factory, so it federates exactly the model that agent is running.
    """
    try:
        from adk.worldmodel import get_world_model
    except ImportError as exc:
        return {"ok": False, "error": f"world model unavailable: {exc}"}

    model = get_world_model(agent_name)
    if model is None:
        # Not an error: the world model is off (AITHER_AGENT_WM unset), which is
        # a deliberate configuration, not a failure to report as one.
        return {"ok": True, "skipped": "world model disabled for this agent"}

    return FederationClient(bearer=bearer).sync(model, allowed_actions=allowed_actions)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="Federate an agent's world model.")
    ap.add_argument("--agent", required=True, help="agent name")
    ap.add_argument("--actions", default="",
                    help="comma-separated shareable action names (required to "
                         "send anything — the export fails closed without it)")
    args = ap.parse_args()
    actions = [a.strip() for a in args.actions.split(",") if a.strip()]
    print(_json.dumps(sync_agent(args.agent, allowed_actions=actions), indent=2))

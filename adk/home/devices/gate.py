"""The device gate: one place where a person's device request becomes an action, a
question, or a refusal -- and a signed receipt either way.

The host (local or hosted Hearth) supplies, per call: the person (``{"pid", "name",
"role"}``, resolved from the authenticated caller, never from a tool argument), the
household policy (:func:`policy.validate_config` output), the approval store (a dict it
persists), whether the session is tainted, and the guardians' pids. The gate supplies
the rules:

* ``act``         run it now; receipt ``device_action`` (or ``device_failed``).
* ``confirm``     return ``waiting_for_you`` with the exact action and its digest; the
                  host puts it on its existing approval card. Only
                  :meth:`DeviceGate.run_confirmed` with that digest, from that person,
                  runs it -- after the policy is asked again.
* ``guarded`` /   create an N-of-M approval (:mod:`.approvals`) bound to the digest,
  ``ask_parent``  tell :func:`.notify.notify_approval`, return ``waiting_for_guardians``.
                  :meth:`DeviceGate.vote` resolves it; quorum runs it once.
* ``refuse``      nothing runs; receipt ``device_refused``.

Every receipt's args carry ``by`` (pid, name, role), the canonical ``action``, its
``digest`` and the ``policy`` decision (class, verdict, reason, tainted, the approvals
rule), so "why did / didn't the door open" is answered from the log.
"""

from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import approvals as ap
from . import policy as pol
from .ha import HAClient, HAError
from .notify import notify_approval

logger = logging.getLogger("adk.home.devices.gate")

Receipt = Callable[[str, str, Any, Any, str], Any]

#: Actions modelled now and built in a later brick: approved, then honestly not run.
NOT_BUILT = {("camera", "record"): "camera recording is not built yet",
             ("camera", pol.CAMERA_SUMMARY): "camera summaries are not built yet"}


def _who(person: Mapping[str, Any]) -> Dict[str, str]:
    return {"pid": str(person.get("pid") or ""), "name": str(person.get("name") or "")[:60],
            "role": str(person.get("role") or "")}


class DeviceGate:
    def __init__(self, client: HAClient, *, household: str, receipt: Receipt,
                 clock: Callable[[], float] = time.time,
                 now: Optional[Callable[[], datetime]] = None):
        self.client = client
        self.household = household
        self._receipt = receipt
        self.clock = clock
        self.now = now or (lambda: datetime.fromtimestamp(self.clock(), timezone.utc))

    async def _log(self, kind: str, name: str, args: Any, result: Any, approval: str) -> None:
        try:
            out = self._receipt(kind, name, args, result, approval)
            if inspect.isawaitable(out):
                await out
        except Exception as exc:  # noqa: BLE001 -- loud, and the caller is told
            logger.error("device receipt %s %s NOT written: %s", kind, name, exc)
            raise

    # ── reads ────────────────────────────────────────────────────────────────
    async def status(self, person: Mapping[str, Any], cfg: Mapping[str, Any],
                     domain: str = "") -> Dict[str, Any]:
        """The devices this person may see, with state and their class."""
        try:
            rows = await self.client.states()
        except HAError as exc:
            return {"error": str(exc)}
        visible = set(pol.exposed_to(person, cfg, [r["entity_id"] for r in rows]))
        dom = str(domain or "").strip().lower()
        out = []
        for r in rows:
            if r["entity_id"] not in visible or (dom and pol.domain_of(r["entity_id"]) != dom):
                continue
            out.append({"entity_id": r["entity_id"], "name": r["name"], "state": r["state"],
                        "class": pol.entity_class(r["entity_id"], r.get("device_class"), cfg),
                        "attributes": r.get("attributes") or {}})
        return {"devices": out[:200], "count": len(out), "via": self.client.last_path,
                "note": "private household data: never put it in a web lookup or a message "
                        "to anyone outside the family"}

    async def _entities(self) -> Dict[str, Dict[str, Any]]:
        rows = await self.client.states()
        return {r["entity_id"]: r for r in rows}

    @staticmethod
    def _unique(entities: Mapping[str, Mapping[str, Any]], ent: Mapping[str, Any]) -> bool:
        dom = pol.domain_of(ent["entity_id"])
        name = str(ent.get("name") or "").strip().lower()
        return bool(name) and sum(
            1 for e in entities.values() if pol.domain_of(e["entity_id"]) == dom
            and str(e.get("name") or "").strip().lower() == name) == 1

    # ── the request ──────────────────────────────────────────────────────────
    async def request(self, person: Mapping[str, Any], cfg: Mapping[str, Any],
                      store: Dict[str, Any], *, domain: Any, action: Any, target: Any,
                      params: Any = None, tainted: bool, guardians: Iterable[str]
                      ) -> Dict[str, Any]:
        who = _who(person)
        try:
            act = pol.normalize_action(domain, action, target, params)
        except pol.PolicyError as exc:
            await self._log("device_refused", "home_act",
                            {"by": who, "raw": {"domain": str(domain)[:64],
                                                "action": str(action)[:64],
                                                "target": str(target)[:160]}},
                            {"error": str(exc)}, "refused:invalid")
            return {"error": f"refused: {exc}"}
        try:
            entities = await self._entities()
        except HAError as exc:
            return {"error": f"{exc}; nothing ran"}
        ent = entities.get(act["target"])
        exposed = pol.exposed_to(person, cfg, entities.keys())
        if ent is None:
            exposed = [e for e in exposed if e != act["target"]]
        d = pol.decide(cfg=cfg, person=person, entity_id=act["target"], action=act["action"],
                       params=act["params"], tainted=tainted, now=self.now(),
                       exposed=exposed, device_class=(ent or {}).get("device_class"))
        digest = pol.action_digest(act, self.household)
        args = {"by": who, "action": act, "digest": digest, "policy": d.as_dict()}
        name = (ent or {}).get("name") or ""
        text = pol.summary(act, name)
        if d.verdict == pol.REFUSE:
            await self._log("device_refused", "home_act", args, {"refused": d.reason},
                            "refused:policy")
            return {"error": f"refused: {d.reason}; nothing ran", "policy": d.as_dict()}
        if d.verdict == pol.ACT:
            return await self._execute(act, ent, entities, d.as_dict(), args, "auto (free)")
        if d.verdict == pol.ASK_ASKER:
            await self._log("device_request", "home_act", args, {"waiting": "asker"},
                            f"asker:{who['pid']}")
            return {"status": "waiting_for_you", "summary": text, "action": act,
                    "digest": digest, "policy": d.as_dict(),
                    "note": "NOT done. It runs only when you say yes to exactly this."}
        guardians = {str(g) for g in guardians}
        # Only a current guardian ever approves; a named approver who is not one is dropped.
        approvers = [p for p in ap.resolve_approvers(d.approvers, guardians) if p in guardians]
        try:
            row = ap.create(store, action=act, household=self.household, verdict=d.verdict,
                            required=d.approvals_required, approvers=approvers,
                            requested_by=who, expires_s=d.expires_s, now=self.clock(),
                            summary=text, policy=d.as_dict())
        except ap.ApprovalError as exc:
            await self._log("device_refused", "home_act", args, {"refused": str(exc)},
                            "refused:no-quorum")
            return {"error": f"refused: {exc}", "policy": d.as_dict()}
        await self._log("device_request", "home_act", args,
                        {"approval": row["id"], "required": row["required"],
                         "approvers": row["approvers"], "expires_at": row["expires_at"]},
                        f"quorum:{row['id']}:0/{row['required']}")
        public = ap.public(row)
        await notify_approval(public)
        status = "asked_a_parent" if d.verdict == pol.ASK_PARENT else "waiting_for_guardians"
        return {"status": status, "approval": public, "summary": text, "policy": d.as_dict(),
                "note": ("NOT done. A parent has been asked." if d.verdict == pol.ASK_PARENT
                         else f"NOT done. It runs once {row['required']} of "
                              f"{len(row['approvers'])} guardians say yes.")}

    async def _execute(self, act: Dict[str, Any], ent: Optional[Mapping[str, Any]],
                       entities: Mapping[str, Mapping[str, Any]], decision: Dict[str, Any],
                       args: Dict[str, Any], approval: str) -> Dict[str, Any]:
        key = (act["domain"], act["action"])
        if key in NOT_BUILT:
            res = {"error": f"{NOT_BUILT[key]}; nothing ran"}
            await self._log("device_failed", "home_act", args, res, approval)
            return {**res, "policy": decision}
        if ent is None:
            res = {"error": f"{act['target']} is not in the home right now; nothing ran"}
            await self._log("device_failed", "home_act", args, res, approval)
            return {**res, "policy": decision}
        try:
            out = await self.client.call(act, entity=ent, cls=decision.get("cls", ""),
                                         unique_name=self._unique(entities, ent))
        except HAError as exc:
            res = {"error": f"{exc}; it may not have happened"}
            await self._log("device_failed", "home_act", args, res, approval)
            return {**res, "policy": decision}
        await self._log("device_action", "home_act", args, out, approval)
        result = {"ok": True, "acted": True, "summary": pol.summary(act, ent.get("name", "")),
                  "via": out.get("via"), "policy": decision}
        if key in pol.TAINTING_ACTIONS:
            result["taints"] = True
        return result

    # ── confirm: the asker's own yes ─────────────────────────────────────────
    async def run_confirmed(self, person: Mapping[str, Any], cfg: Mapping[str, Any],
                            action: Mapping[str, Any], digest: str, *,
                            tainted: bool) -> Dict[str, Any]:
        """The asker said yes on the card. The policy is asked AGAIN now: only a
        decision that is still ``confirm`` for this person (or now ``act``) runs, and
        only the action whose digest is the one on the card."""
        who = _who(person)
        try:
            act = pol.normalize_action(action.get("domain"), action.get("action"),
                                       action.get("target"), action.get("params"))
        except pol.PolicyError as exc:
            return {"error": f"refused: {exc}"}
        if pol.action_digest(act, self.household) != str(digest or ""):
            await self._log("device_refused", "home_act", {"by": who, "action": act},
                            {"refused": "digest mismatch"}, "refused:digest")
            return {"error": "refused: that is not the action you said yes to; nothing ran"}
        try:
            entities = await self._entities()
        except HAError as exc:
            return {"error": f"{exc}; nothing ran"}
        ent = entities.get(act["target"])
        d = pol.decide(cfg=cfg, person=person, entity_id=act["target"], action=act["action"],
                       params=act["params"], tainted=tainted, now=self.now(),
                       exposed=pol.exposed_to(person, cfg, entities.keys()),
                       device_class=(ent or {}).get("device_class"))
        args = {"by": who, "action": act, "digest": digest, "policy": d.as_dict()}
        if d.verdict not in (pol.ASK_ASKER, pol.ACT):
            await self._log("device_refused", "home_act", args,
                            {"refused": f"now {d.verdict}"}, "refused:policy-changed")
            return {"error": f"refused: this now needs {d.verdict.replace('_', ' ')}; "
                             "nothing ran", "policy": d.as_dict()}
        return await self._execute(act, ent, entities, d.as_dict(), args,
                                    f"asker:yes:{who['pid']}")

    # ── guarded / ask-parent: votes ─────────────────────────────────────────
    async def vote(self, person: Mapping[str, Any], cfg: Mapping[str, Any],
                   store: Dict[str, Any], approval_id: str, allow: bool, *,
                   guardians: Iterable[str]) -> Dict[str, Any]:
        who = _who(person)
        row = ap.get(store, approval_id)
        if row is None:
            return {"error": "no such request; nothing changed"}
        # Who counts NOW: the approvers fixed at creation who are still guardians.
        eligible = sorted(set(row["approvers"]) & {str(g) for g in guardians})
        try:
            ap.vote(row, pid=who["pid"], allow=allow, now=self.clock(), eligible=eligible)
        except ap.ApprovalError as exc:
            return {"error": str(exc), "approval": ap.public(row)}
        await self._log("device_vote", "home_act",
                        {"by": who, "approval": row["id"], "digest": row["digest"],
                         "allow": bool(allow)},
                        {"status": row["status"], "approvals": row["approvals"],
                         "required": row["required"]},
                        f"quorum:{row['id']}:{row['approvals']}/{row['required']}")
        if row["status"] == ap.PENDING:
            # every guardian's card shows the new count ("Approved by Sam · 1 of 2")
            await notify_approval(ap.public(row))
            return {"ok": True, "ran": False, "approval": ap.public(row)}
        if row["status"] != ap.APPROVED:
            await notify_approval(ap.public(row))
            return {"ok": True, "ran": False, "approval": ap.public(row)}
        try:
            act = ap.claim_for_run(row, self.clock())
        except ap.ApprovalError as exc:
            return {"error": str(exc), "approval": ap.public(row)}
        try:
            entities = await self._entities()
        except HAError as exc:
            ap.finish(row, False, {"error": str(exc)}, self.clock())
            await notify_approval(ap.public(row))
            return {"error": f"{exc}; nothing ran", "approval": ap.public(row)}
        ent = entities.get(act["target"])
        voters = sorted(p for p, v in row["votes"].items() if v["allow"])
        args = {"by": row["requested_by"], "action": act, "digest": row["digest"],
                "policy": row["policy"], "approved_by": voters, "approval": row["id"]}
        out = await self._execute(act, ent, entities, row["policy"], args,
                                  f"quorum:{row['id']}:{row['approvals']}/{row['required']}")
        ap.finish(row, bool(out.get("ok")), {k: v for k, v in out.items() if k != "policy"},
                  self.clock())
        await notify_approval(ap.public(row))
        return {"ok": True, "ran": bool(out.get("ok")), "result": out,
                "approval": ap.public(row)}

    def pending(self, store: Mapping[str, Any], pid: str) -> List[Dict[str, Any]]:
        return ap.pending_for(store, pid, self.clock())

"""CreateEngine: the six pillars as one closed loop, behind one front door.

    intent -> context -> reasoning -> (approve) -> orchestration -> creation -> learning

``plan()`` runs the first three and PREVIEWS the fourth. ``approve()`` runs the
fourth (dispatch), the fifth (build + deploy + verify) and the sixth (learning),
which writes what happened back into the memory plane so the NEXT ``plan()``'s
context stage recalls it and its prediction moves. Every stage appends one entry
to ``plan["trace"]``; a plan always shows all six pillars.

The engine owns no provisioning. Each kind in :mod:`adk.create_flow.kinds` names
the route or tool that already creates that thing, and the engine only drives it.

Isolation: a plan belongs to ``principal.user_id``. Reads and writes by any other
principal answer "not found". The tenant/workspace used for a build is the one the
BACKEND derives from the caller's credentials, never a field of the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .backends import Backend, MemoryFanout
from .kinds import KINDS, PILLARS, SLUG_RE, Ctx, KindError, infer_kind, slugify
from .store import PlanStore

logger = logging.getLogger("adk.create_flow")

LESSON_CATEGORY = "create_flow"
LESSON_MARK = "create_flow lesson "
_WS_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

#: Used until the platform has seen this kind before. Deliberately modest.
#: A "running" plan with no live task and no update for this long was killed by a restart.
STALE_RUN_S = 600
MAX_RUNNING = 3

DEFAULT_PRIOR = {"p_success": 0.8, "duration_s": 6.0}

#: (kind, option) pairs that need a decision card because they are irreversible,
#: spend money, or publish externally. Not wired in this slice: planning refuses them.
CARD_OPTIONS = ("publish", "spend_usd", "public")

LLMComplete = Callable[[list], Awaitable[str]]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as the transport resolved it."""

    user_id: str
    tenant_id: str = ""


class PlanError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> float:
    return time.time()


class CreateEngine:
    def __init__(
        self,
        *,
        runtime: str,
        store: PlanStore,
        backend: Optional[Backend] = None,
        memory: Optional[MemoryFanout] = None,
        pack_root: Optional[Path] = None,
        llm_complete: Optional[LLMComplete] = None,
        card_gate: Optional[Callable[[dict[str, Any]], Awaitable[Optional[str]]]] = None,
    ):
        self.runtime = runtime  # "genesis" | "awdk"
        self.store = store
        self.backend = backend
        self.memory = memory
        self.pack_root = Path(pack_root) if pack_root else None
        self.llm_complete = llm_complete
        self.card_gate = card_gate
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        #: True when this engine exists for one background build: the run closes the backend.
        self.owns_backend = False

    # ── trace ───────────────────────────────────────────────────────────────

    @staticmethod
    def _stage(plan: dict[str, Any], pillar: str, status: str, detail: str, **extra: Any) -> None:
        entry = {"pillar": pillar, "status": status, "detail": detail, "at": _now(), **extra}
        trace = plan["trace"]
        for i, old in enumerate(trace):
            if old["pillar"] == pillar and old["status"] in ("pending", "running", "planned"):
                entry["started"] = old.get("started", old["at"])
                trace[i] = entry
                break
        else:
            trace.append(entry)
        plan["updated_at"] = entry["at"]

    @staticmethod
    def _blank_trace() -> list[dict[str, Any]]:
        return [{"pillar": p, "status": "pending", "detail": "", "at": 0.0} for p in PILLARS]

    def _ctx(self, plan: dict[str, Any], docs: Optional[list[dict[str, str]]] = None) -> Ctx:
        return Ctx(
            plan=plan,
            backend=self.backend,
            pack_root=self.pack_root,
            docs=docs if docs is not None else plan.get("docs", []),
        )

    # ── 1-3: plan ───────────────────────────────────────────────────────────

    async def plan(
        self,
        principal: Principal,
        *,
        kind: str = "",
        description: str = "",
        name: str = "",
        workspace_id: str = "",
        docs: Optional[list[dict[str, str]]] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        description = (description or "").strip()
        options = dict(options or {})
        docs = [
            {"name": str(d.get("name", "attachment"))[:80], "text": str(d.get("text", ""))[:20000]}
            for d in (docs or [])[:5]
            if isinstance(d, dict)
        ]
        if not description and not name:
            raise PlanError("Say what you want in a sentence.")
        if not principal.tenant_id or principal.tenant_id.lower() in ("public", "unknown"):
            raise PlanError("Create needs a signed-in workspace account.", 403)
        if workspace_id and not _WS_ID.match(workspace_id):
            raise PlanError("That workspace id is not valid.")
        if len(json.dumps(options, default=str)) > 6000:
            raise PlanError("Those options are too large.")
        skills_opt = options.get("skills")
        if skills_opt is not None and (not isinstance(skills_opt, list) or len(skills_opt) > 10):
            raise PlanError("skills must be a list of at most 10 names.")
        try:
            options["effort_cap"] = max(1, min(10, int(options.get("effort_cap") or 6)))
        except (TypeError, ValueError):
            raise PlanError("effort_cap must be a number from 1 to 10.")
        options["soul_text"] = str(options.get("soul_text") or "")[:4000]
        options["tools"] = [str(t)[:64] for t in (options.get("tools") or [])][:30]
        mine = self.store.list(principal.user_id, limit=500)
        if sum(1 for p in mine if p.get("status") == "running") >= MAX_RUNNING:
            raise PlanError("Three builds are already running; wait for one to finish.", 429)
        if len(description) > 8000:
            raise PlanError("That description is too long; keep it under 8000 characters.")
        for opt in CARD_OPTIONS:
            if options.get(opt):
                raise PlanError(
                    f"'{opt}' is irreversible, paid or public. That goes through a decision card, "
                    "which Create does not raise yet. Remove it to plan the private version.",
                    422,
                )

        plan: dict[str, Any] = {
            "id": self.store.new_id(principal.user_id),
            "owner": principal.user_id,
            "runtime": self.runtime,
            "status": "planning",
            "created_at": _now(),
            "updated_at": _now(),
            "description": description,
            "options": options,
            "docs": docs,
            "scope": {"tenant_id": principal.tenant_id, "workspace_id": workspace_id},
            "trace": self._blank_trace(),
            "steps": [],
            "result": None,
            "verify": None,
            "undo": None,
        }
        if not principal.user_id:
            raise PlanError("Not signed in.", 401)

        # 1. INTENT -----------------------------------------------------------
        t0 = _now()
        inferred = infer_kind(f"{name} {description}")
        chosen = kind or inferred
        if chosen not in KINDS:
            raise PlanError(
                "Pick what to create: workspace, agent, bot, workflow, agent pack, "
                "tool pack or skill pack."
            )
        decision = await self._classify(description or name)
        title = name.strip() or self._title_from(description, chosen)
        slug = slugify(options.get("slug") or title)
        plan.update(kind=chosen, name=title, slug=slug)
        agree = (not inferred) or inferred == chosen
        self._stage(
            plan,
            "intent",
            "done",
            f"{KINDS[chosen].label}: {title!r}"
            + ("" if agree else f" (your words also sound like a {KINDS[inferred].label.lower()})"),
            intent=decision,
            kind=chosen,
            inferred_kind=inferred,
            ms=int((_now() - t0) * 1000),
        )

        # 2. CONTEXT ----------------------------------------------------------
        t0 = _now()
        handler = KINDS[chosen]
        lessons = await self._lessons(chosen)
        ctx = self._ctx(plan, docs)
        existing = await handler.existing(ctx)
        plan["context"] = {
            "lessons": len(lessons),
            "existing": existing[:50],
            "docs": [d["name"] for d in docs],
        }
        collision = slug in existing
        self._stage(
            plan,
            "context",
            "done",
            f"recalled {len(lessons)} earlier {handler.label.lower()} lesson(s); "
            f"{len(existing)} existing; "
            f"{len(docs)} document(s) read" + ("; THE NAME IS TAKEN" if collision else ""),
            ms=int((_now() - t0) * 1000),
        )

        # 3. REASONING --------------------------------------------------------
        t0 = _now()
        preview = handler.draft(self._ctx(plan, docs))
        blockers = list(preview.pop("problems", []))
        preflight = getattr(handler, "preflight", None)
        if preflight is not None:
            blockers += await preflight(ctx)
        risks: list[str] = []
        if not SLUG_RE.match(slug) and chosen in ("workspace", "agent", "skill_pack"):
            blockers.append(
                f"the name {slug!r} is not a valid short name (letters, digits, dashes, 2-32 chars)"
            )
        if collision:
            blockers.append(f"{slug!r} already exists here; pick another name")
        if chosen == "agent" and not plan["scope"]["workspace_id"]:
            risks.append(
                "no workspace chosen: the agent will belong to your tenant, not one workspace"
            )
        if not handler.executable:
            blockers.append(handler.reason)
        risks = blockers + risks + list(plan.pop("unchecked", []))
        prediction = self._predict(lessons, chosen)
        preview["risks"] = risks
        plan["preview"] = preview
        plan["prediction"] = prediction
        plan["blocked"] = bool(blockers)
        preview["blockers"] = blockers
        self._stage(
            plan,
            "reasoning",
            "done",
            f"{len(preview['steps'])} step(s); "
            f"predicted {int(prediction['p_success'] * 100)}% success in "
            f"~{prediction['duration_s']:.0f}s ({prediction['basis']})"
            + ("; blocked: " + blockers[0] if blockers else ""),
            ms=int((_now() - t0) * 1000),
        )

        # 4. ORCHESTRATION (previewed; dispatched on approve) -------------------
        route = self._route(plan, handler)
        plan["route"] = route
        self._stage(
            plan,
            "orchestration",
            "planned",
            f"will run on {route['runtime']} via {route['path']}; agent: {route['agent']}",
        )
        plan["steps"] = [{"label": s, "status": "pending", "at": 0.0} for s in preview["steps"]]
        plan["status"] = "blocked" if plan["blocked"] else "planned"
        plan["undo"] = {"available": handler.executable, "how": handler.undo_note}
        self.store.save(plan)
        self.store.prune(principal.user_id)
        return self._public(plan)

    async def _classify(self, text: str) -> dict[str, Any]:
        from adk import intent

        try:
            if self.llm_complete is not None:
                d = await intent.classify_intent(text, llm_complete=self.llm_complete)
            else:
                d = intent.keyword_intent(text)
            return d.as_dict()
        except Exception as exc:  # noqa: BLE001 - intent must never block creating
            logger.debug("intent classify failed: %s", exc)
            return {"intent": "creation", "source": "default"}

    @staticmethod
    def _title_from(description: str, kind: str) -> str:
        first = description.strip().splitlines()[0] if description.strip() else kind
        first = first.rstrip(".!? ")
        for lead in (
            "make me a ",
            "make me an ",
            "create a ",
            "create an ",
            "build me a ",
            "i want a ",
            "i need a ",
        ):
            if first.lower().startswith(lead):
                first = first[len(lead) :]
                break
        return first[:60] or kind

    def _route(self, plan: dict[str, Any], handler: Any) -> dict[str, Any]:
        where = plan["preview"]["where"]
        agent = {
            "workspace": "atlas (provisioning)",
            "agent": "demiurge (identity authoring)",
            "bot": "demiurge (declarative bot scaffolding)",
            "skill_pack": "scribe (skill drafting)",
            "tool_pack": "demiurge (pack authoring)",
        }.get(plan["kind"], "n/a")
        return {
            "runtime": where["runtime"]
            if self.runtime == "genesis"
            else f"{self.runtime} -> platform API",
            "path": handler.path,
            "agent": agent,
            "durable_on_awnix": where.get("durable_on_awnix", False),
        }

    # ── learning's read side ─────────────────────────────────────────────────

    async def _lessons(self, kind: str) -> list[dict[str, Any]]:
        if self.memory is None:
            return []
        try:
            texts = await self.memory.recall(f"{LESSON_MARK}kind={kind}", LESSON_CATEGORY)
        except Exception as exc:  # noqa: BLE001
            logger.debug("lesson recall failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for t in texts:
            i = t.find("{")
            if LESSON_MARK not in t or i < 0:
                continue
            try:
                row = json.loads(t[i:])
            except ValueError:
                continue
            row = self._clean_lesson(row, kind)
            if row is not None:
                out.append(row)
        return out

    @staticmethod
    def _clean_lesson(row: Any, kind: str) -> Optional[dict[str, Any]]:
        """Memory is writable by every member of the tenant: accept only well-typed rows."""
        if not isinstance(row, dict) or row.get("kind") != kind:
            return None
        if row.get("event") not in ("run", "undo"):
            return None
        try:
            actual = row.get("actual_s")
            return {
                "event": row["event"],
                "kind": kind,
                "ok": bool(row.get("ok")),
                "actual_s": None if actual is None else max(0.0, min(float(actual), 86400.0)),
            }
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _predict(lessons: list[dict[str, Any]], kind: str) -> dict[str, Any]:
        runs = [x for x in lessons if x.get("event") == "run"]
        if not runs:
            return {**DEFAULT_PRIOR, "basis": "default prior; no earlier runs", "n": 0}
        ok = sum(1 for x in runs if x.get("ok"))
        times = [
            float(x["actual_s"]) for x in runs if x.get("ok") and x.get("actual_s") is not None
        ]
        # Beta prior worth two runs centred on the default: never 0 or 1 from a handful of runs.
        p = (ok + 2 * DEFAULT_PRIOR["p_success"]) / (len(runs) + 2)
        dur = sum(times) / len(times) if times else DEFAULT_PRIOR["duration_s"]
        return {
            "p_success": round(p, 3),
            "duration_s": round(dur, 2),
            "basis": f"{ok}/{len(runs)} earlier {kind} runs",
            "n": len(runs),
        }

    # ── 4-6: approve ─────────────────────────────────────────────────────────

    def _mine(self, principal: Principal, plan_id: str) -> dict[str, Any]:
        """The plan, only for the user AND tenant that made it; a stale run is failed here."""
        plan = self.store.load(plan_id)
        if (
            not plan
            or plan.get("owner") != principal.user_id
            or plan.get("scope", {}).get("tenant_id", "") != principal.tenant_id
        ):
            raise PlanError("No such plan.", 404)
        if (
            plan.get("status") == "running"
            and plan_id not in self._tasks
            and _now() - float(plan.get("updated_at", 0)) > STALE_RUN_S
        ):
            plan["status"] = "failed"
            plan["error"] = (
                "The build was interrupted (the service restarted). Undo it or plan again."
            )
            self.store.save(plan)
        return plan

    def get(self, principal: Principal, plan_id: str) -> dict[str, Any]:
        return self._public(self._mine(principal, plan_id))

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        return [self._public(p, brief=True) for p in self.store.list(principal.user_id)]

    async def approve(
        self, principal: Principal, plan_id: str, *, wait: bool = False
    ) -> dict[str, Any]:
        plan = self._mine(principal, plan_id)
        if plan["status"] != "planned":
            raise PlanError(f"This plan is {plan['status']}, so it cannot be approved.", 409)
        handler = KINDS[plan["kind"]]
        if not handler.executable:
            raise PlanError(handler.reason, 501)
        if self.card_gate is not None:
            card = await self.card_gate(plan)
            if card:
                plan["status"], plan["card_id"] = "awaiting_decision", card
                self.store.save(plan)
                return self._public(plan)
        running = [
            p for p in self.store.list(principal.user_id, limit=500) if p.get("status") == "running"
        ]
        if len(running) >= MAX_RUNNING:
            raise PlanError("Three builds are already running; wait for one to finish.", 429)
        claimed = self.store.claim(plan_id, "planned", "running")
        if claimed is None:
            raise PlanError("This plan was already approved.", 409)
        plan = claimed
        plan["approved_at"] = _now()
        self.store.save(plan)
        task = asyncio.get_running_loop().create_task(self._run(plan_id))
        self._tasks[plan_id] = task
        task.add_done_callback(lambda _t, pid=plan_id: self._tasks.pop(pid, None))
        if wait:
            await task
        return self._public(self.store.load(plan_id) or plan)

    async def _run(self, plan_id: str) -> None:
        """Never leave a plan in "running": any escape from the build marks it failed."""
        try:
            await self._run_inner(plan_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("create run crashed")
            plan = self.store.load(plan_id)
            if plan is not None and plan.get("status") == "running":
                plan["status"] = "failed"
                plan["error"] = f"{type(exc).__name__}: {exc}"[:300]
                self._stage(plan, "creation", "failed", plan["error"])
                self.store.save(plan)
        finally:
            close = getattr(self.backend, "aclose", None)
            if self.owns_backend and close is not None:
                await close()

    async def _run_inner(self, plan_id: str) -> None:
        plan = self.store.load(plan_id)
        if plan is None:
            return
        handler = KINDS[plan["kind"]]
        ctx = self._ctx(plan)
        started = _now()
        ok, error = False, ""

        def save() -> None:
            self.store.save(plan)

        # 4. ORCHESTRATION: dispatch
        self._stage(
            plan,
            "orchestration",
            "done",
            f"dispatched to {plan['route']['agent']} on {plan['route']['runtime']}",
        )
        self._stage(plan, "creation", "running", "building")
        save()
        try:
            for step in plan["steps"][:1]:
                step.update(status="running", at=_now())
            save()
            plan["result"] = await handler.execute(ctx)
            if plan["steps"]:
                plan["steps"][0].update(status="done", at=_now())
            for step in plan["steps"][1:]:
                step.update(status="running", at=_now())
                save()
            checks = await handler.verify(ctx)
            for step in plan["steps"][1:]:
                step.update(status="done", at=_now())
            plan["verify"] = {"ok": all(c["ok"] for c in checks), "checks": checks}
            ok = plan["verify"]["ok"]
            if not ok:
                error = "verification failed: " + "; ".join(
                    c["name"] for c in checks if not c["ok"]
                )
        except KindError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface, never hang in "running"
            logger.exception("create run failed")
            error = f"{type(exc).__name__}: {exc}"[:300]
        elapsed = _now() - started
        if ok:
            self._stage(
                plan,
                "creation",
                "done",
                "built, deployed and verified: "
                + ", ".join(c["name"] for c in plan["verify"]["checks"]),
                ms=int(elapsed * 1000),
            )
            final = "deployed"
        else:
            for step in plan["steps"]:
                if step["status"] in ("pending", "running"):
                    step["status"] = "failed" if step["status"] == "running" else "skipped"
            self._stage(plan, "creation", "failed", error, ms=int(elapsed * 1000))
            final = "failed"
            plan["error"] = error
        save()
        # Terminal status only AFTER learning: a poller that sees "deployed" sees all six pillars.
        await self._learn(plan, ok=ok, actual_s=elapsed, error=error, event="run")
        plan["status"] = final
        save()

    @staticmethod
    def _error_kind(error: str) -> str:
        """What goes into shared memory is a category, never text a user or a route typed."""
        if not error:
            return ""
        low = error.lower()
        for needle, kind in (
            ("refused", "refused"),
            ("verification", "verification"),
            ("sign in", "no_connection"),
            ("validate", "invalid"),
        ):
            if needle in low:
                return kind
        return "failed"

    # ── 6. LEARNING ──────────────────────────────────────────────────────────

    async def _learn(
        self, plan: dict[str, Any], *, ok: bool, actual_s: float, error: str, event: str
    ) -> None:
        pred = plan.get("prediction", {})
        lesson = {
            "event": event,
            "kind": plan["kind"],
            "slug": plan["slug"],
            "plan_id": plan["id"],
            "runtime": plan["runtime"],
            "ok": ok,
            "actual_s": round(actual_s, 3),
            "predicted_p": pred.get("p_success"),
            "predicted_s": pred.get("duration_s"),
            "error": self._error_kind(error),
            "at": _now(),
        }
        # Brier term: how wrong was the success prediction on this run.
        if pred.get("p_success") is not None:
            lesson["brier"] = round((float(pred["p_success"]) - (1.0 if ok else 0.0)) ** 2, 4)
        sinks: list[str] = []
        if self.memory is not None:
            body = json.dumps(lesson, sort_keys=True)
            text = f"{LESSON_MARK}kind={plan['kind']} event={event} ok={ok} {body}"
            try:
                sinks = list(await self.memory.remember(LESSON_CATEGORY, text) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("lesson write failed: %s", exc)
        plan["learning"] = {"lesson": lesson, "sinks": sinks}
        detail = (
            f"predicted {int((pred.get('p_success') or 0) * 100)}% / "
            f"~{pred.get('duration_s', 0):.0f}s, "
            f"got {'success' if ok else 'failure'} in {actual_s:.1f}s; "
            + (f"written to {', '.join(sinks)}" if sinks else "NOT written: no memory plane is up")
        )
        self._stage(plan, "learning", "done" if sinks else "degraded", detail, sinks=sinks)

    # ── undo ──────────────────────────────────────────────────────────────────

    async def undo(self, principal: Principal, plan_id: str) -> dict[str, Any]:
        plan = self._mine(principal, plan_id)
        if plan["status"] not in ("deployed", "failed"):
            raise PlanError(
                f"This plan is {plan['status']}; only a deployed or failed one can be undone.", 409
            )
        if not plan.get("result"):
            raise PlanError("Nothing was created, so there is nothing to undo.", 409)
        handler = KINDS[plan["kind"]]
        started = _now()
        try:
            out = await handler.undo(self._ctx(plan))
        except KindError as exc:
            raise PlanError(str(exc), 502)
        plan["status"] = "undone"
        plan["undo"] = {"available": False, "done": True, "result": out, "at": _now()}
        self.store.save(plan)
        await self._learn(plan, ok=True, actual_s=_now() - started, error="", event="undo")
        self.store.save(plan)
        return self._public(plan)

    # ── presentation ──────────────────────────────────────────────────────────

    @staticmethod
    def _public(plan: dict[str, Any], brief: bool = False) -> dict[str, Any]:
        out = {k: v for k, v in plan.items() if k not in ("docs",)}
        if brief:
            for k in ("preview", "context", "learning", "options"):
                out.pop(k, None)
        return out

    async def wait_idle(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

"""``/create/*`` on the harness daemon: the SAME Create engine as Genesis, run locally.

The engine (:mod:`adk.create_flow`) is runtime-agnostic: it takes a backend and a
memory plane. Here the backend is the platform API reached with the owner's own bearer
(``HttpBackend``), the pack directory is the owner's creator packs dir, and memory fans
out to the local SQLite memory AND the platform memory plane when the bearer exists.
So the same request plans and builds whether it lands on Genesis or on this daemon, and
the Learning step writes to memory either way.

Routes mirror ``routers/create.py``: ``GET /create/kinds``, ``POST /create/plan``,
``GET /create/plans``, ``GET /create/plans/{id}``, ``POST /create/plans/{id}/approve``,
``POST /create/plans/{id}/undo``. Auth is the daemon's own ``auth`` dependency; the
daemon is single-owner by construction, so the principal is the box owner.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.harnesses.create_routes")

__all__ = ["mount", "build_engine"]

_BEARER_FILE = Path.home() / ".aither" / "session-bearer"


def _bearer() -> str:
    env = os.environ.get("AITHER_SESSION_BEARER", "").strip()
    if env:
        return env
    try:
        return _BEARER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _platform_base() -> str:
    explicit = (
        os.environ.get("AITHER_CREATE_PLATFORM_URL") or os.environ.get("AITHER_GENESIS_URL") or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")
    return "https://api.aitherium.com/api/genesis"


def _state_dir() -> Path:
    env = os.environ.get("AITHER_CREATE_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".aither" / "create"


def build_engine(state: Optional[Path] = None, bearer: Optional[str] = None):
    from adk.create_flow import CreateEngine, PlanStore
    from adk.create_flow.backends import (
        HttpBackend,
        LocalMemory,
        MemoryFanout,
        PlatformMemory,
    )

    state = Path(state) if state else _state_dir()
    token = _bearer() if bearer is None else bearer
    backend = HttpBackend(_platform_base(), token) if token else None
    planes: List[Any] = [LocalMemory(state / "memory.db")]
    if backend is not None:
        planes.append(PlatformMemory(backend))
    packs = os.environ.get("AITHER_CREATOR_PACKS_DIR", "").strip()
    return CreateEngine(
        runtime="awdk",
        store=PlanStore(state / "plans"),
        backend=backend,
        memory=MemoryFanout(planes),
        pack_root=(
            Path(packs).expanduser() if packs else Path.home() / ".aither" / "packs" / "creator"
        ),
    )


def mount(app: Any, auth: Any) -> None:
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel, Field

    from adk.create_flow import KINDS, Principal
    from adk.create_flow.engine import PlanError

    class Doc(BaseModel):
        name: str = Field(default="attachment", max_length=80)
        text: str = Field(default="", max_length=20000)

    class PlanBody(BaseModel):
        kind: str = ""
        description: str = Field(default="", max_length=8000)
        name: str = Field(default="", max_length=80)
        workspace_id: str = Field(default="", max_length=80)
        docs: List[Doc] = Field(default_factory=list, max_length=5)
        options: Dict[str, Any] = Field(default_factory=dict)

    holder: Dict[str, Any] = {}
    principal = Principal(user_id="local-owner", tenant_id="local")

    def engine():
        # Rebuilt per call so a re-login (new bearer) is picked up without a restart.
        eng = build_engine()
        prev = holder.get("engine")
        if prev is not None:
            eng._tasks = prev._tasks
        holder["engine"] = eng
        return eng

    def fail(exc: PlanError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @app.get("/create/kinds", dependencies=[Depends(auth)])
    def kinds() -> Dict[str, Any]:
        return {
            "kinds": [
                {
                    "key": k.key,
                    "label": k.label,
                    "executable": k.executable,
                    "path": k.path,
                    "undo": k.undo_note,
                    "reason": getattr(k, "reason", ""),
                }
                for k in KINDS.values()
            ]
        }

    @app.post("/create/plan", dependencies=[Depends(auth)])
    async def plan(body: PlanBody) -> Dict[str, Any]:
        try:
            return await engine().plan(
                principal,
                kind=body.kind,
                description=body.description,
                name=body.name,
                workspace_id=body.workspace_id,
                docs=[d.model_dump() for d in body.docs],
                options=body.options,
            )
        except PlanError as exc:
            raise fail(exc)

    @app.get("/create/plans", dependencies=[Depends(auth)])
    def plans() -> Dict[str, Any]:
        return {"plans": engine().list(principal)}

    @app.get("/create/plans/{plan_id}", dependencies=[Depends(auth)])
    def get_plan(plan_id: str) -> Dict[str, Any]:
        try:
            return engine().get(principal, plan_id)
        except PlanError as exc:
            raise fail(exc)

    @app.post("/create/plans/{plan_id}/approve", status_code=202, dependencies=[Depends(auth)])
    async def approve(plan_id: str) -> Dict[str, Any]:
        try:
            return await engine().approve(principal, plan_id)
        except PlanError as exc:
            raise fail(exc)

    @app.post("/create/plans/{plan_id}/undo", dependencies=[Depends(auth)])
    async def undo(plan_id: str) -> Dict[str, Any]:
        try:
            return await engine().undo(principal, plan_id)
        except PlanError as exc:
            raise fail(exc)

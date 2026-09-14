"""Agent heartbeat + self-programmed routines.

A :class:`RoutineStore` is a durable registry of cron-scheduled self-prompts:
the agent writes an instruction for its FUTURE self ("check the market every
morning and warn me if prices spike"), the store persists it
(``~/.aither/routines/{agent}.json``), rehydrates it into the existing
:class:`adk.cron.CronScheduler` on :meth:`start`, and a fire runs
``agent.chat(instruction)`` with the agent's full toolset — which IS
self-programming with its own adk (composable with skill auto-learning).

Guardrails (the leash principle):
    - ``max_routines`` per agent (default 12) and a minimum fire interval
      (default 5 minutes) — a routine never fires again within the window
      regardless of its cron expression.
    - Every fire is bounded by a per-fire timeout and its result is LEDGERED
      to ``last_result`` (truncated), visible via ``routine_list``.
    - The self-management tool handlers ONLY touch the RoutineStore — they can
      never modify agent config, safety gates, or the LLM backend.

Routines with a DIRECT callable (``register_direct``) fire a bound method
instead of a self-prompt — used by ``AitherAgent(memory_maintenance=True)`` so
memory upkeep runs even on tiny models, while staying visible/manageable via
the same tools.

Everything is opt-in: nothing constructs a RoutineStore unless a caller does.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("adk.routines")

DEFAULT_MAX_ROUTINES = 12
DEFAULT_MIN_INTERVAL_S = 300.0       # 5 minutes between fires of one routine
DEFAULT_FIRE_TIMEOUT_S = 300.0       # per-fire wall clock bound
DEFAULT_RESULT_MAX_CHARS = 2000      # last_result ledger truncation


@dataclass
class Routine:
    """One durable scheduled routine."""

    name: str
    cron: str
    instruction: str
    enabled: bool = True
    last_run: Optional[str] = None       # ISO-8601 UTC
    last_result: Optional[str] = None    # truncated fire result / error
    tags: List[str] = field(default_factory=list)
    direct: bool = False                 # fires a bound method, not a self-prompt
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Routine":
        return cls(**{
            k: v for k, v in dict(data or {}).items()
            if k in cls.__dataclass_fields__
        })


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _from_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class RoutineStore:
    """Durable, cron-scheduled routine registry for ONE agent.

    Args:
        agent_name: Namespaces the on-disk registry file.
        path: Explicit registry file path (overrides the default
            ``$AITHER_DATA_DIR|~/.aither/routines/{agent}.json``).
        fire: Async (or sync) callable ``(instruction: str) -> str`` invoked on
            a self-prompt routine fire — typically a wrapper around
            ``agent.chat``. Direct routines bypass it.
        scheduler: Injectable ``CronScheduler`` (tests); built lazily otherwise.
        clock: Injectable ``() -> float`` epoch clock (tests).
    """

    def __init__(
        self,
        agent_name: str = "default",
        path: str | Path | None = None,
        *,
        fire: Optional[Callable[[str], Any]] = None,
        scheduler: Any = None,
        max_routines: int = DEFAULT_MAX_ROUTINES,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        fire_timeout_s: float = DEFAULT_FIRE_TIMEOUT_S,
        result_max_chars: int = DEFAULT_RESULT_MAX_CHARS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.agent_name = agent_name
        if path is None:
            base = Path(
                os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
            ) / "routines"
            path = base / f"{agent_name}.json"
        self._path = Path(path)
        self._fire_cb = fire
        self._scheduler = scheduler
        self._max_routines = int(max_routines)
        self._min_interval_s = float(min_interval_s)
        self._fire_timeout_s = float(fire_timeout_s)
        self._result_max = int(result_max_chars)
        self._clock = clock or time.time
        self._routines: Dict[str, Routine] = {}
        self._direct: Dict[str, Callable[[], Any]] = {}
        self._started = False
        self._load()

    # ─── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.warning("routine registry unreadable (%s): %s", self._path, exc)
            return
        for entry in data if isinstance(data, list) else []:
            try:
                r = Routine.from_dict(entry)
            except TypeError:
                continue
            if r.name:
                self._routines[r.name] = r

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([r.to_dict() for r in self._routines.values()],
                           indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("routine registry save failed (%s): %s", self._path, exc)

    # ─── CRUD ────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_cron(expression: str) -> None:
        from adk.cron import _cron_matches
        _cron_matches(expression, datetime.now(tz=timezone.utc))  # raises ValueError

    def create(
        self,
        name: str,
        cron: str,
        instruction: str,
        tags: Optional[List[str]] = None,
        enabled: bool = True,
        *,
        direct: bool = False,
        _system: bool = False,
    ) -> Routine:
        """Register a new routine. Raises ValueError on a duplicate name, a bad
        cron expression, or when ``max_routines`` is reached (system-registered
        maintenance routines bypass the cap but still count toward it).
        Raises LicenseError if cron routines are not licensed."""
        # Fail-closed: cron routines require BUILDER+ tier (or INTERNAL/SOVEREIGN)
        if not _system:  # system routines bypass the license gate
            try:
                from adk.licensing import get_license_manager
                get_license_manager().require("cron", friendly="Cron routines")
            except ImportError:
                pass  # licensing unavailable; proceed without gate

        name = str(name).strip()
        if not name:
            raise ValueError("Routine name must not be empty")
        if name in self._routines:
            raise ValueError(f"Routine {name!r} already exists")
        if not _system and len(self._routines) >= self._max_routines:
            raise ValueError(
                f"Routine limit reached ({self._max_routines}); delete one first"
            )
        self._validate_cron(cron)
        r = Routine(
            name=name, cron=cron, instruction=str(instruction),
            enabled=bool(enabled), tags=list(tags or []), direct=bool(direct),
            created_at=_iso(self._clock()),
        )
        self._routines[name] = r
        self._save()
        if self._started and r.enabled:
            self._schedule(r)
        return r

    def get(self, name: str) -> Optional[Routine]:
        return self._routines.get(name)

    def list(self) -> List[Routine]:
        return list(self._routines.values())

    def update(
        self,
        name: str,
        cron: Optional[str] = None,
        instruction: Optional[str] = None,
        tags: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
    ) -> Routine:
        r = self._routines.get(name)
        if r is None:
            raise ValueError(f"Unknown routine {name!r}")
        if cron is not None and cron != "":
            self._validate_cron(cron)
            r.cron = cron
        if instruction is not None and instruction != "":
            r.instruction = str(instruction)
        if tags is not None:
            r.tags = list(tags)
        if enabled is not None:
            r.enabled = bool(enabled)
        self._save()
        if self._started:
            self._unschedule(name)
            if r.enabled:
                self._schedule(r)
        return r

    def pause(self, name: str) -> Routine:
        return self.update(name, enabled=False)

    def resume(self, name: str) -> Routine:
        return self.update(name, enabled=True)

    def delete(self, name: str) -> bool:
        if name not in self._routines:
            return False
        del self._routines[name]
        self._direct.pop(name, None)
        self._save()
        if self._started:
            self._unschedule(name)
        return True

    def register_direct(
        self,
        name: str,
        fn: Callable[[], Any],
        cron: str,
        instruction: str,
        tags: Optional[List[str]] = None,
    ) -> Routine:
        """Attach a DIRECT callable routine (a bound method fire, not a
        self-prompt). Creates the routine if missing; when it already exists
        (rehydrated from disk, possibly user-edited) the persisted
        cron/enabled state is KEPT and only the callable is re-attached."""
        r = self._routines.get(name)
        if r is None:
            r = self.create(
                name, cron, instruction, tags=tags, direct=True, _system=True,
            )
        else:
            r.direct = True
            if not r.instruction:
                r.instruction = instruction
            self._save()
        self._direct[name] = fn
        if self._started and r.enabled:
            self._unschedule(name)
            self._schedule(r)
        return r

    # ─── scheduler wiring ────────────────────────────────────────────────────

    def _ensure_scheduler(self):
        if self._scheduler is None:
            from adk.cron import CronScheduler
            cron_dir = self._path.parent / f"{self.agent_name}.cron"
            self._scheduler = CronScheduler(data_dir=cron_dir)
        return self._scheduler

    def _schedule(self, r: Routine) -> None:
        sched = self._ensure_scheduler()
        try:
            sched.add(r.cron, self._task_for(r.name), name=r.name)
        except ValueError:
            # already present (rehydration overlap) — replace
            try:
                sched.remove(r.name)
                sched.add(r.cron, self._task_for(r.name), name=r.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("routine schedule failed for %s: %s", r.name, exc)

    def _unschedule(self, name: str) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.remove(name)
            except Exception:  # noqa: BLE001
                pass

    def _task_for(self, name: str) -> Callable[[], Any]:
        def task():
            return self._fire(name, scheduled=True)
        return task

    async def start(self) -> None:
        """Rehydrate every enabled routine into the CronScheduler and start it.
        The RoutineStore is the source of truth — stale persisted cron stubs
        are cleared first."""
        if self._started:
            return
        sched = self._ensure_scheduler()
        await sched.start()
        for job in list(sched.list_jobs()):
            sched.remove(job.name)
        for r in self._routines.values():
            if r.enabled:
                self._schedule(r)
        self._started = True
        logger.info(
            "routine heartbeat started for %s (%d routines)",
            self.agent_name, sum(1 for r in self._routines.values() if r.enabled),
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            try:
                await self._scheduler.stop()
            except Exception:  # noqa: BLE001
                pass
        self._started = False

    # ─── firing ──────────────────────────────────────────────────────────────

    async def run_now(self, name: str) -> str:
        """Fire a routine immediately (explicit ask — bypasses the min-interval
        guard but keeps the timeout + result ledger)."""
        return await self._fire(name, scheduled=False)

    async def _fire(self, name: str, scheduled: bool = True) -> str:
        r = self._routines.get(name)
        if r is None:
            return f"skipped: unknown routine {name!r}"
        if not r.enabled:
            return "skipped: routine is paused"
        now = self._clock()
        if scheduled:
            last = _from_iso(r.last_run)
            if last is not None and (now - last) < self._min_interval_s:
                logger.debug("routine %s skipped (min interval)", name)
                return "skipped: min-interval guard"

        direct_fn = self._direct.get(name)
        try:
            if direct_fn is not None:
                out = direct_fn()
            elif self._fire_cb is not None:
                out = self._fire_cb(r.instruction)
            else:
                out = "error: no fire callback configured"
            if inspect.isawaitable(out):
                out = await asyncio.wait_for(out, timeout=self._fire_timeout_s)
            result = "" if out is None else str(out)
        except asyncio.TimeoutError:
            result = f"timeout: fire exceeded {self._fire_timeout_s:.0f}s"
        except Exception as exc:  # noqa: BLE001 — a routine must never crash the loop
            result = f"error: {exc}"

        r.last_run = _iso(now)
        r.last_result = result[: self._result_max]
        self._save()
        return r.last_result


# ─────────────────────────────────────────────────────────────────────────────
# Self-management tools — OpenAI tool defs + handlers.
# LEASH: every handler below closes over the RoutineStore ONLY. None of them
# can reach agent config, safety gates, or the LLM backend.
# ─────────────────────────────────────────────────────────────────────────────


def _split_tags(tags: str) -> List[str]:
    return [t.strip() for t in str(tags or "").split(",") if t.strip()]


def build_routine_tools(store: RoutineStore):
    """Build the routine self-management tools bound to *store*.

    Returns a :class:`adk.tools.ToolRegistry` containing
    ``routine_create/list/update/pause/resume/delete/run_now``.
    """
    from adk.tools import ToolRegistry

    def _ok(**payload) -> str:
        return json.dumps({"ok": True, **payload}, ensure_ascii=False)

    def _err(exc: Exception) -> str:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def routine_create(name: str, cron: str, instruction: str, tags: str = "") -> str:
        """Create a recurring routine: a 5-field cron schedule plus the instruction your future self will run at each fire.

        name: Short unique routine name.
        cron: 5-field cron expression (minute hour day month weekday), e.g. "0 9 * * *" for daily at 09:00 UTC.
        instruction: The instruction to run on each fire (you will receive it as a chat message with your full toolset).
        tags: Optional comma-separated tags.
        """
        try:
            r = store.create(name, cron, instruction, tags=_split_tags(tags))
            return _ok(routine=r.to_dict())
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def routine_list() -> str:
        """List every routine with schedule, enabled state, last run time and last (truncated) result."""
        return _ok(routines=[r.to_dict() for r in store.list()])

    def routine_update(name: str, cron: str = "", instruction: str = "", tags: str = "") -> str:
        """Update a routine's cron expression, instruction and/or tags (empty fields are left unchanged).

        name: Routine to update.
        cron: New 5-field cron expression, or empty to keep.
        instruction: New instruction, or empty to keep.
        tags: New comma-separated tags, or empty to keep.
        """
        try:
            kwargs: Dict[str, Any] = {}
            if cron:
                kwargs["cron"] = cron
            if instruction:
                kwargs["instruction"] = instruction
            if tags:
                kwargs["tags"] = _split_tags(tags)
            r = store.update(name, **kwargs)
            return _ok(routine=r.to_dict())
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def routine_pause(name: str) -> str:
        """Pause a routine (it stays registered but stops firing)."""
        try:
            return _ok(routine=store.pause(name).to_dict())
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def routine_resume(name: str) -> str:
        """Resume a paused routine."""
        try:
            return _ok(routine=store.resume(name).to_dict())
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def routine_delete(name: str) -> str:
        """Delete a routine permanently."""
        try:
            return _ok(deleted=store.delete(name))
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    async def routine_run_now(name: str) -> str:
        """Fire a routine immediately and return its (truncated) result."""
        try:
            return _ok(result=await store.run_now(name))
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    registry = ToolRegistry()
    for fn in (routine_create, routine_list, routine_update, routine_pause,
               routine_resume, routine_delete, routine_run_now):
        registry.register(fn)
    return registry


def routine_tool_defs(store: Optional[RoutineStore] = None) -> List[dict]:
    """OpenAI function-calling definitions for the routine tools (for hosts
    that wire tools manually instead of via ``register_routine_tools``)."""
    if store is None:
        import tempfile
        import uuid as _uuid
        # Schema-only store: the path never gets created (no writes happen).
        store = RoutineStore(
            agent_name="_schema_only",
            path=Path(tempfile.gettempdir())
            / f"adk-routine-schema-{_uuid.uuid4().hex}.json",
        )
    return build_routine_tools(store).to_openai_format()


def register_routine_tools(agent: Any, store: RoutineStore) -> List[str]:
    """Register the routine self-management tools onto *agent*'s tool registry.
    Returns the registered tool names."""
    registry = build_routine_tools(store)
    names: List[str] = []
    for td in registry.list_tools():
        agent._tools._tools[td.name] = td
        names.append(td.name)
    return names

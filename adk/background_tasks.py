"""Full-duplex async tools: start a slow tool now, read its result on a later step.

The ReAct loop is turn-synchronous: one slow tool call stalls the whole turn because
the model cannot decide anything until the observation comes back. This module adds
the escape hatch DeepSeek Harness calls ``start()``/``stop()`` tools:

* ``bg_start`` wraps ANY already-registered tool (name + args) and returns at once
  with a task id. The tool keeps running while the turn continues.
* When it finishes, fails, times out or is stopped, ONE completion event is appended
  to the turn's ``adk.turn_control.EscalationController.events`` queue. If no turn
  owns the registry at that moment the event waits in a small pending queue and is
  flushed into the next turn that binds a sink.
* The loop drains the background events at the top of every step and hands the model
  ``[background task <id> <name> finished: <clipped result>]`` as a new message, so
  the NEXT model step sees it.
* ``bg_status`` returns the full stored result (Inline & Spill: the first
  ``inline_chars`` of a result ride in the event; the whole thing stays addressable
  by task id).
* ``bg_stop`` cancels a running task.

Two safety rules make backgrounding behave like the foreground loop:

* a tool the approval policy gates never starts without an allow already recorded
  for that exact call in this session — the card must be shown by calling it
  directly, so ``bg_start`` refuses with ``approval_required`` instead;
* the caller's ``AuthContext`` (bound per turn with :func:`bind_turn`) is passed
  through to the tool boundary, so a restricted principal cannot escalate by
  backgrounding a tool it may not call.

Off unless the agent is built with ``background_tools=True``, so every existing
agent behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("adk.background_tasks")

#: The turn's caller identity. A ContextVar, not registry state: asyncio copies the
#: context into every task the loop spawns, so two concurrent turns sharing one agent
#: can never inherit each other's principal or session.
_TURN_SESSION: contextvars.ContextVar = contextvars.ContextVar(
    "adk_bg_turn_session", default=None)
_TURN_AUTH: contextvars.ContextVar = contextvars.ContextVar(
    "adk_bg_turn_auth", default=None)


def bind_turn(*, session_id: str | None = None, auth: Any = None) -> None:
    """Bind the caller's session/auth for tool starts made during this turn.

    The agent loops call this at the top of every turn with the principal they were
    handed. ``None`` (the defaults) means "no caller identity", which is exactly how
    :meth:`adk.tools.ToolRegistry.execute` already treats ``auth=None``.
    """
    _TURN_SESSION.set(session_id)
    _TURN_AUTH.set(auth)


def reset_turn() -> None:
    """Clear the bound turn context (tests, or a host that wants a clean slate)."""
    _TURN_SESSION.set(None)
    _TURN_AUTH.set(None)

#: Chars of a result that ride inline in the completion message.
INLINE_CHARS = 600
#: Chars of a result kept addressable through ``bg_status`` (memory bound).
FULL_RESULT_MAX_CHARS = 20_000
#: Concurrent running tasks allowed per agent before ``start`` refuses.
DEFAULT_MAX_CONCURRENT = 4
#: Default wall-clock budget for one background task.
DEFAULT_TIMEOUT_S = 300.0
#: Completion events kept while no turn is bound (a flood must not grow forever).
PENDING_EVENT_CAP = 200
#: Finished records kept for ``bg_status`` history.
HISTORY_CAP = 200
#: Tools that must never be backgrounded (they would recurse or hide the harness).
_NEVER_BACKGROUNDED = frozenset({"bg_start", "bg_stop", "bg_status"})

RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
TIMEOUT = "timeout"
STOPPED = "stopped"


class BackgroundTaskError(Exception):
    """A request that cannot be honoured (bad name, unknown task, no event loop)."""


class BackgroundTaskLimitError(BackgroundTaskError):
    """The concurrent-task cap is reached; the caller must wait or stop one."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 3:
        return text[: max(0, limit)]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _task_state(handle: Any) -> str:
    """``"created"`` while a task's coroutine has never started; ``""`` otherwise."""
    try:
        coro = handle.get_coro()
        if inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
            return "created"
    except (AttributeError, TypeError, ValueError):
        return ""
    return ""


def _close_coroutine(action: Any) -> None:
    """Close a coroutine that will never be awaited (no RuntimeWarning, no leak)."""
    if not inspect.iscoroutine(action):
        return
    try:
        action.close()
    except RuntimeError as exc:  # already running / finished
        logger.debug("background coroutine close skipped: %s", exc)


def _parse_timeout(value: Any) -> float | None:
    """``None`` when unset, else a positive float. Raises :class:`BackgroundTaskError`.

    Done BEFORE the wrapped tool's coroutine exists, so a malformed ``timeout_s``
    cannot leave that coroutine created-but-never-awaited.
    """
    if value is None:
        return None
    try:
        budget = float(value)
    except (TypeError, ValueError) as exc:
        raise BackgroundTaskError("timeout_s must be a number") from exc
    if budget <= 0:
        raise BackgroundTaskError("timeout must be > 0")
    return budget


def _approval_refusal(agent_name: str, tool_name: str, args: dict) -> str | None:
    """Fail closed when a gated tool would run with no Allow/Deny card.

    The foreground loops (:meth:`adk.agent.AitherAgent.chat` and ``stream_react``)
    pause before a gated tool so the customer can decide. A background start has no
    place to pause, so the only safe outcomes are: run it because an allow was
    already recorded for THIS call in this session, or refuse and tell the model to
    call the tool directly. Returns the model-readable JSON error, or ``None`` when
    the tool may start.
    """
    try:
        from adk.approval import get_approval_store, needs_approval

        gated = needs_approval(agent_name, tool_name, args)
    except Exception as exc:  # noqa: BLE001 -- an unreadable gate must not run
        logger.warning("background approval check failed for %s: %s", tool_name, exc)
        gated = True
    if not gated:
        return None

    session_id = _TURN_SESSION.get()
    decision: str | None = None
    if session_id:
        try:
            decision = get_approval_store().decision_for(session_id, tool_name, args)
        except Exception as exc:  # noqa: BLE001 -- unreadable decisions mean "ask"
            logger.warning("background decision lookup failed for %s: %s", tool_name, exc)
    if decision == "allow":
        return None
    return json.dumps({
        "error": "approval_required",
        "tool": tool_name,
        "message": ("this tool needs the user's Allow/Deny decision; call it directly "
                    "instead of through bg_start so the turn can ask"),
    })


@dataclass
class BackgroundTask:
    """State of one background tool run."""

    task_id: str
    name: str
    status: str = RUNNING
    started_at: float = 0.0
    finished_at: float = 0.0
    timeout_s: float = DEFAULT_TIMEOUT_S
    inline: str = ""
    full: str = ""
    error: str = ""
    truncated: bool = False
    handle: Any = None
    payload: Any = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    emitted: bool = False

    def summary(self, now: float) -> dict:
        end = self.finished_at or now
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status,
            "elapsed_s": round(max(0.0, end - self.started_at), 3),
            "timeout_s": self.timeout_s,
            "chars": len(self.full),
            "error": self.error[:300],
        }


class BackgroundTasks:
    """Registry of detached tool runs for one agent.

    Args:
        max_concurrent: running tasks allowed before :meth:`start` refuses.
        default_timeout: wall-clock budget used when ``start(..., timeout=None)``.
        inline_chars: chars of a result carried in the completion event.
        full_chars: chars of a result kept addressable through ``status``.
        clock: injectable for tests; only used for timestamps.

    ``start`` is synchronous and returns the task id. The coroutine runs on the
    event loop that is running when ``start`` is called; a caller outside a loop
    gets :class:`BackgroundTaskError` rather than a task that never runs.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        default_timeout: float = DEFAULT_TIMEOUT_S,
        inline_chars: int = INLINE_CHARS,
        full_chars: int = FULL_RESULT_MAX_CHARS,
        clock=time.time,
    ) -> None:
        if max_concurrent < 1:
            raise BackgroundTaskError("max_concurrent must be >= 1")
        self.max_concurrent = int(max_concurrent)
        self.default_timeout = float(default_timeout)
        self.inline_chars = int(inline_chars)
        self.full_chars = int(full_chars)
        self._clock = clock
        self._tasks: dict[str, BackgroundTask] = {}
        self._pending: list[dict] = []
        self._sink: list[dict] | None = None

    # -- lifecycle -----------------------------------------------------------------

    def start(self, name: str, action: Any, *, timeout: float | None = None) -> str:
        """Schedule ``action`` and return its task id immediately.

        ``action`` is a coroutine/awaitable or a zero-argument callable (sync or
        async). Raises :class:`BackgroundTaskLimitError` when the cap is reached and
        :class:`BackgroundTaskError` for an unusable name or a missing loop.
        """
        clean = (name or "").strip()
        if not clean:
            raise BackgroundTaskError("background task needs a non-empty name")
        if not (callable(action) or inspect.isawaitable(action)):
            raise BackgroundTaskError("action must be a coroutine or a callable")
        if self._running_count() >= self.max_concurrent:
            raise BackgroundTaskLimitError(
                f"too many background tasks ({self.max_concurrent} running); "
                "stop one with bg_stop or wait for one to finish")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise BackgroundTaskError(
                "background tasks need a running async event loop") from exc

        # Same rule as bg_start: a malformed budget is a refused start, never a
        # raw ValueError that would leave a caller's coroutine unawaited.
        parsed = _parse_timeout(timeout)
        budget = self.default_timeout if parsed is None else parsed

        task_id = uuid.uuid4().hex[:12]
        record = BackgroundTask(
            task_id=task_id,
            name=clean[:80],
            started_at=self._clock(),
            timeout_s=budget,
            payload=action,
        )
        self._tasks[task_id] = record
        record.handle = loop.create_task(self._run(record, action, budget))
        self._prune_history()
        return task_id

    async def _run(self, record: BackgroundTask, action: Any, timeout: float) -> None:
        try:
            result = await asyncio.wait_for(self._invoke(action), timeout)
        except asyncio.CancelledError:
            if record.status == RUNNING:
                record.status = STOPPED
                record.error = "stopped"
            self._finish(record)
            raise
        except asyncio.TimeoutError:
            record.status = TIMEOUT
            record.error = f"timed out after {timeout:g}s"
        except Exception as exc:  # noqa: BLE001 -- a tool fault is a result, not a crash
            record.status = FAILED
            record.error = f"{type(exc).__name__}: {exc}"
        else:
            record.status = FINISHED
            raw = _text(result)
            record.truncated = len(raw) > self.full_chars
            record.full = _clip(raw, self.full_chars) if record.truncated else raw
            record.inline = _clip(raw, self.inline_chars)
        finally:
            self._close_unstarted(action)
        self._finish(record)

    async def _invoke(self, action: Any) -> Any:
        if inspect.isawaitable(action):
            return await action
        result = action() if callable(action) else action
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _close_unstarted(action: Any) -> None:
        """Close a coroutine that was cancelled before its first await."""
        _close_coroutine(action)

    def _finish(self, record: BackgroundTask) -> None:
        if record.emitted:
            return
        record.emitted = True
        record.finished_at = record.finished_at or self._clock()
        record.done.set()
        self._emit(record)
        self._prune_history()

    # -- stop / status -------------------------------------------------------------

    def stop(self, task_id: str) -> bool:
        """Cancel a running task. False when unknown, finished or already stopped."""
        record = self._tasks.get(task_id)
        if record is None or record.status != RUNNING:
            return False
        record.status = STOPPED
        record.error = "stopped"
        handle = record.handle
        if handle is not None and not handle.done():
            # A task cancelled before its first step never reaches ``_run``, so its
            # payload coroutine would be GC'd unawaited; close it here.
            never_started = _task_state(handle) == "created"
            handle.cancel()
            if never_started:
                self._close_unstarted(record.payload)
        # Finish here rather than waiting for the cancellation to unwind: the stop
        # has already decided the outcome, and ``_run`` may never get a step.
        self._finish(record)
        return True

    def status(self, task_id: str | None = None) -> Any:
        """One task's full state, or a summary of every task when id is None.

        Unknown ids return ``{"error": "unknown_task", "task_id": <id>}`` so a tool
        can hand the answer straight back to the model.
        """
        now = self._clock()
        if task_id is None:
            return {
                "running": self._running_count(),
                "max_concurrent": self.max_concurrent,
                "tasks": [r.summary(now) for r in self._tasks.values()],
            }
        record = self._tasks.get(task_id)
        if record is None:
            return {"error": "unknown_task", "task_id": task_id}
        out = record.summary(now)
        out.update({
            "result": record.full,
            "inline": record.inline,
            "truncated": record.truncated,
        })
        return out

    async def wait(self, task_id: str, timeout: float | None = None) -> Any:
        """Await one task's completion event, then return :meth:`status`."""
        record = self._tasks.get(task_id)
        if record is None:
            raise BackgroundTaskError(f"unknown background task: {task_id}")
        waiter = record.done.wait()
        if timeout is not None:
            await asyncio.wait_for(waiter, timeout)
        else:
            await waiter
        return self.status(task_id)

    # -- event plumbing ------------------------------------------------------------

    def bind_events(self, sink: list[dict] | None) -> None:
        """Point completions at a turn's events list and flush what already finished.

        Events of ours that the PREVIOUS sink never consumed are carried forward, so
        a result that lands in the gap between two turns is not lost. ``None``
        unbinds: completions from now on wait in :meth:`drain_events`.
        """
        if self._sink is not None and self._sink is not sink:
            self._pending.extend(
                e for e in self._sink if isinstance(e, dict) and e.get("type") == "background")
            self._sink[:] = [
                e for e in self._sink if not (isinstance(e, dict)
                                              and e.get("type") == "background")]
            self._cap_pending()
        self._sink = sink
        if sink is not None and self._pending:
            sink.extend(self._pending)
            self._pending.clear()

    def drain_events(self) -> list[dict]:
        """Return and clear completion events that no sink has taken yet."""
        out, self._pending = self._pending, []
        return out

    def _emit(self, record: BackgroundTask) -> None:
        event = {
            "type": "background",
            "task_id": record.task_id,
            "name": record.name,
            "status": record.status,
            "text": _event_text(record),
        }
        if self._sink is not None:
            self._sink.append(event)
            return
        self._pending.append(event)
        self._cap_pending()

    # -- helpers -------------------------------------------------------------------

    def _cap_pending(self) -> None:
        if len(self._pending) > PENDING_EVENT_CAP:
            del self._pending[: len(self._pending) - PENDING_EVENT_CAP]

    def _running_count(self) -> int:
        return sum(1 for r in self._tasks.values() if r.status == RUNNING)

    def _prune_history(self) -> None:
        finished = [k for k, r in self._tasks.items() if r.status != RUNNING]
        overflow = len(finished) - HISTORY_CAP
        if overflow <= 0:
            return
        for key in finished[:overflow]:
            self._tasks.pop(key, None)


def _event_text(record: BackgroundTask) -> str:
    head = f"[background task {record.task_id} {record.name}"
    if record.status == FINISHED:
        return f"{head} finished: {record.inline or '(no output)'}]"
    if record.status == TIMEOUT:
        return (f"{head} timed out after {record.timeout_s:g}s: "
                f"{record.inline or '(no output)'}]")
    if record.status == STOPPED:
        return f"{head} stopped]"
    return f"{head} failed: {_clip(record.error, INLINE_CHARS) or '(no error)'}]"


def register_background_tools(agent: Any, registry: BackgroundTasks | None = None) -> int:
    """Register ``bg_start``/``bg_stop``/``bg_status`` on an agent's tool registry.

    Returns how many tools were registered. Refuses when the agent has no
    :class:`BackgroundTasks` bound — the feature is opt-in, never implicit.
    """
    registry = registry if registry is not None else getattr(agent, "_background", None)
    if registry is None:
        raise BackgroundTaskError("agent has no BackgroundTasks registry")
    tools = getattr(agent, "_tools", None)
    if tools is None:
        raise BackgroundTaskError("agent has no tool registry")
    agent_name = str(getattr(agent, "name", "") or "")

    async def bg_start(tool: str, args: dict | None = None,
                       timeout_s: float | None = None) -> str:
        """Run a registered tool in the background; returns a task id immediately."""
        name = str(tool or "").strip()
        if not name:
            return json.dumps({"error": "bg_start needs a tool name"})
        if name in _NEVER_BACKGROUNDED:
            return json.dumps({"error": f"{name} cannot be backgrounded"})
        if tools.get(name) is None:
            return json.dumps({"error": f"Unknown tool: {name}"})
        # Models commonly serialize an object argument to a JSON string (the tool
        # schema for a dict hint degrades to "string"); accept both shapes.
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                return json.dumps({"error": "args must be an object/dict"})
        if args is not None and not isinstance(args, dict):
            return json.dumps({"error": "args must be an object/dict"})
        # Validate the budget BEFORE the tool coroutine exists: a bad timeout must
        # not leave a created-but-never-awaited coroutine behind.
        try:
            budget = _parse_timeout(timeout_s)
        except BackgroundTaskError as exc:
            return json.dumps({"error": "cannot_start", "message": str(exc)})
        # Human-in-the-loop gate: backgrounding must never route around the
        # Allow/Deny card the foreground loop shows for a gated tool.
        refusal = _approval_refusal(agent_name, name, dict(args or {}))
        if refusal is not None:
            return refusal
        # Carry the caller's principal to the tool boundary (G23), exactly as the
        # foreground loop does; no bound context means the usual auth=None call.
        auth = _TURN_AUTH.get()
        if auth is not None:
            call = tools.execute(name, dict(args or {}), auth=auth)
        else:
            call = tools.execute(name, dict(args or {}))
        try:
            task_id = registry.start(name, call, timeout=budget)
        except BackgroundTaskLimitError as exc:
            _close_coroutine(call)
            return json.dumps({"error": "at_capacity", "message": str(exc)})
        except BackgroundTaskError as exc:
            _close_coroutine(call)
            return json.dumps({"error": "cannot_start", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- never leave a coroutine unawaited
            _close_coroutine(call)
            logger.warning("background start for %s failed: %s", name, exc)
            return json.dumps({"error": "cannot_start", "message": str(exc)})
        return json.dumps({
            "task_id": task_id,
            "tool": name,
            "status": RUNNING,
            "timeout_s": budget if budget is not None else registry.default_timeout,
        })

    async def bg_stop(task_id: str) -> str:
        """Cancel a running background task by its task id."""
        tid = str(task_id or "").strip()
        if not tid:
            return json.dumps({"error": "bg_stop needs a task id"})
        stopped = registry.stop(tid)
        out: dict = {"task_id": tid, "stopped": stopped}
        if not stopped:
            current = registry.status(tid)
            out["status"] = current.get("status") if isinstance(current, dict) else None
            if isinstance(current, dict) and current.get("error") == "unknown_task":
                out["error"] = "unknown_task"
        return json.dumps(out)

    async def bg_status(task_id: str = "") -> str:
        """Read a background task's full result, or list every task when id is empty."""
        tid = str(task_id or "").strip()
        return json.dumps(registry.status(tid or None), default=str)

    registered = 0
    for fn, tool_name, desc, intents in (
        (bg_start, "bg_start",
         "Start a registered tool in the background and get a task id back at once; "
         "its result arrives on a later step. Use for slow tools.",
         ["command", "code", "analysis"]),
        (bg_stop, "bg_stop", "Cancel a running background task by task id.",
         ["command"]),
        (bg_status, "bg_status",
         "Read a background task's full result (or list all tasks with no id).",
         ["command", "analysis", "question"]),
    ):
        try:
            tools.register(fn, name=tool_name, description=desc,
                           intent_categories=list(intents))
            registered += 1
        except Exception as exc:  # noqa: BLE001 -- one bad tool must not hide the rest
            logger.warning("background tool %s not registered: %s", tool_name, exc)
    return registered

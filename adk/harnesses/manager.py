"""The session manager — the actual "shell for shells".

Owns every live harness session on this host: creates them, routes turns to
them, fans their events out, and reaps them when they exit. One manager backs
all three front-ends (the AitherShell CLI, the AitherShell web app, and the
portal), so a session started on the desktop is the SAME session a phone
attaches to through the tunnel — not a copy.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from adk.harnesses.events import notice
from adk.harnesses.models import ModelBinding, ProfileError, resolve_binding
from adk.harnesses.registry import SPECS, HarnessSpec, Transport, detect, get
from adk.harnesses.session import (
    HarnessSession,
    SessionConfig,
    SessionState,
    read_events_page,
    resume_context,
    session_root,
)

#: Sessions kept after exit so a client can still read the final transcript.
REAP_AFTER_SECONDS = float(os.environ.get("AITHER_HARNESS_REAP_SECONDS", "1800"))

#: Refuse to start more than this many concurrent sessions. Each is a real
#: coding agent with a model budget; an unbounded count is a runaway bill.
MAX_SESSIONS = int(os.environ.get("AITHER_HARNESS_MAX_SESSIONS", "24"))

#: How many trailing log events a woken session is handed as resume context.
WAKE_TAIL_EVENTS = 200

#: A session id that may name a directory under the session root. Ids are minted as
#: 16 hex chars; anything with a separator or a dot is refused before a path is built.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DEAD = (SessionState.EXITED, SessionState.FAILED)


class ManagerError(RuntimeError):
    """A session could not be created. Always surfaced — never defaulted away."""


class SessionManager:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._sessions: dict[str, HarnessSession] = {}
        self._lock = threading.RLock()
        self._root = root
        self._exited_at: dict[str, float] = {}
        #: Serializes wake() so two concurrent wakes of one id spawn ONE process.
        self._wake_lock = threading.Lock()

    # ── creation ────────────────────────────────────────────────────────────

    def create(self, config: SessionConfig, *, rows: int = 30, cols: int = 100) -> HarnessSession:
        session = self._build(config, rows=rows, cols=cols)
        with self._lock:
            self._sessions[session.id] = session
        session.persist_session_file()
        session.start()
        return session

    def _check_capacity(self) -> None:
        with self._lock:
            live = [s for s in self._sessions.values() if s.state not in _DEAD]
            if len(live) >= MAX_SESSIONS:
                raise ManagerError(
                    f"session limit reached ({MAX_SESSIONS} live). Close one first."
                )

    def _build(
        self, config: SessionConfig, *, rows: int = 30, cols: int = 100, session_id: str = "",
    ) -> HarnessSession:
        """Construct (never start) the session object for ``config``."""
        try:
            spec: HarnessSpec = get(config.harness)
        except KeyError as exc:
            raise ManagerError(str(exc)) from exc
        self._check_capacity()

        binding: Optional[ModelBinding] = None
        if config.model_profile:
            if not spec.supports_model_binding:
                raise ManagerError(
                    f"harness '{spec.id}' does not support model profiles; "
                    "pass an explicit model instead"
                )
            try:
                binding = resolve_binding(config.model_profile)
            except ProfileError as exc:
                raise ManagerError(str(exc)) from exc

        if config.cwd:
            cwd = Path(config.cwd).expanduser()
            if not cwd.is_dir():
                raise ManagerError(f"cwd does not exist: {cwd}")
            config.cwd = str(cwd)

        if spec.transport == Transport.PTY_STREAM:
            from adk.harnesses.pty_session import PtyHarnessSession

            session: HarnessSession = PtyHarnessSession(
                spec, config, binding, root=self._root, rows=rows, cols=cols,
                session_id=session_id,
            )
        elif spec.id == "group":
            from adk.harnesses.agents import GroupSession

            participants = config.participants or ["aither"]
            session = GroupSession(
                spec, config, binding, root=self._root,
                participants=participants, base_url=config.base_url, session_id=session_id,
            )
        elif spec.transport == Transport.HTTP_STREAM:
            from adk.harnesses.agents import ADK_URL, AgentRelaySession

            # `awdk` relays to this host's own loop unless the caller named a
            # base_url; `aither` keeps Genesis (an explicit base_url still wins).
            base_url = config.base_url or (ADK_URL if spec.id == "awdk" else "")
            session = AgentRelaySession(
                spec, config, binding, root=self._root,
                agent=config.agent or "aither", base_url=base_url, session_id=session_id,
            )
        else:
            session = HarnessSession(spec, config, binding, root=self._root,
                                     session_id=session_id)
        return session

    # ── wake: re-spawn a dead session from its own log ──────────────────────

    def _session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id or ""):
            raise ManagerError(f"invalid session id: {session_id!r}")
        return session_root(self._root) / session_id

    def _stored_config(self, session_id: str, head: list[dict]) -> SessionConfig:
        """The config a session was started with: ``session.json``, else its first event.

        Sessions started before ``session.json`` existed still carry the harness and
        cwd on their ``session.starting`` event; that is enough to re-spawn them.
        """
        path = self._session_dir(session_id) / "session.json"
        known = {f.name for f in dataclasses.fields(SessionConfig)}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8")).get("config") or {}
                return SessionConfig(**{k: v for k, v in raw.items() if k in known})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise ManagerError(
                    f"session {session_id}: unreadable session.json: {exc}"
                ) from exc
        for ev in head:
            if ev.get("kind") == "session.starting":
                data = ev.get("data") or {}
                if data.get("harness"):
                    return SessionConfig(harness=str(data["harness"]),
                                         cwd=str(data.get("cwd") or ""))
        raise ManagerError(
            f"session {session_id}: no session.json and no session.starting event"
        )

    def _log_tail(self, session_id: str, tail: int) -> tuple[list[dict], list[dict], int]:
        """``(head, tail_events, last_seq)`` of a session log, read in bounded pages."""
        transcript = self._session_dir(session_id) / "events.jsonl"
        if not transcript.is_file():
            raise ManagerError(f"no such session: {session_id} (no events.jsonl on this host)")
        head: list[dict] = []
        window: list[dict] = []
        after, more = 0, True
        while more:
            page, more = read_events_page(transcript, after)
            if not page:
                break
            if not head:
                head = page[:8]
            window = (window + page)[-max(tail, 1):]
            after = int(page[-1]["seq"])
        return head, (window if tail > 0 else []), after

    def wake(self, session_id: str, *, tail: int = WAKE_TAIL_EVENTS) -> dict[str, Any]:
        """Re-spawn a dead harness session from its own ``events.jsonl``. Idempotent.

        A live session answers ``{"woken": False}`` and is left alone. A dead one (an
        exited process, or a session the daemon no longer holds because it restarted or
        reaped it) is re-created under the SAME id: its seq continues from the log's last
        event, its harness-native session id goes back as ``--resume`` where the harness
        supports it, and the log tail rides in as resume context.
        """
        with self._wake_lock:
            with self._lock:
                current = self._sessions.get(session_id)
            if current is not None and current.state not in _DEAD:
                return {"woken": False, "already_live": True, "session": current.info()}
            head, events, last_seq = self._log_tail(session_id, tail)
            original = self._stored_config(session_id, head)
            harness_sid = ""
            for ev in reversed(events or head):
                harness_sid = str((ev.get("data") or {}).get("harness_session_id") or "")
                if harness_sid:
                    break
            config = dataclasses.replace(original)
            context = resume_context(events)
            if context:
                block = (f'<resumed-session id="{session_id}" after_seq="{last_seq}">\n'
                         f"{context}\n</resumed-session>")
                config.system_prompt_append = (
                    (original.system_prompt_append or "").rstrip() + "\n\n" + block
                ).strip()
            try:
                resumable = get(config.harness).supports_resume
            except KeyError as exc:
                raise ManagerError(str(exc)) from exc
            if harness_sid and resumable and not config.resume_session_id:
                config.resume_session_id = harness_sid
            if config.cwd and not Path(config.cwd).is_dir():
                raise ManagerError(f"cannot wake {session_id}: cwd is gone: {config.cwd}")
            session = self._build(config, session_id=session_id)
            session.persist_config = original
            session.resume_from(last_seq, harness_sid)
            with self._lock:
                self._sessions[session_id] = session
                self._exited_at.pop(session_id, None)
            session.persist_session_file()
            session._emit(notice(
                "session woken from its log",
                wake=True, after_seq=last_seq, tail_events=len(events),
                resumed_harness_session=harness_sid,
            ))
            session.start()
            return {"woken": True, "already_live": False, "resumed_from_seq": last_seq,
                    "tail_events": len(events), "session": session.info()}

    def read_events(self, session_id: str, *, after: int = 0, limit: int = 0) -> dict[str, Any]:
        """THE positional read of one harness session's log, live or not.

        A session this manager holds answers from its buffer or its log; one it does
        not hold (daemon restarted, reaped) answers from ``events.jsonl`` on disk.
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            page, more = session.events_page(after, limit)
            state, last = session.state, session.last_seq
        else:
            transcript = self._session_dir(session_id) / "events.jsonl"
            if not transcript.is_file():
                raise ManagerError(f"no such session: {session_id}")
            page, more = read_events_page(transcript, after, limit)
            state = "dormant"
            last = int(page[-1]["seq"]) if page else after
        next_after = int(page[-1]["seq"]) if page else after
        return {"session_id": session_id, "events": page, "next_after": next_after,
                "more": more, "last_seq": last, "state": state}

    def resize(self, session_id: str, rows: int, cols: int) -> bool:
        """Resize a terminal session. False for harnesses with no window."""
        session = self.get_session(session_id)
        resizer = getattr(session, "resize", None)
        if resizer is None:
            return False
        return bool(resizer(rows, cols))

    # ── access ──────────────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> HarnessSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ManagerError(f"no such session: {session_id}")
        return session

    def list_sessions(self, owner: str = "") -> list[dict[str, Any]]:
        self.reap()
        with self._lock:
            sessions = list(self._sessions.values())
        infos = [s.info() for s in sessions]
        if owner:
            infos = [i for i in infos if i.get("owner") == owner]
        return sorted(infos, key=lambda i: i["created_at"])

    def send(self, session_id: str, text: str) -> bool:
        return self.get_session(session_id).send(text)

    def interrupt(self, session_id: str) -> bool:
        return self.get_session(session_id).interrupt()

    def stop(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        session.stop()
        with self._lock:
            self._exited_at[session_id] = time.time()
        return session.info()

    def stop_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop()
        return len(sessions)

    # ── housekeeping ────────────────────────────────────────────────────────

    def reap(self) -> int:
        """Drop long-exited sessions from memory. Transcripts stay on disk."""
        now = time.time()
        removed = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.state not in (SessionState.EXITED, SessionState.FAILED):
                    continue
                marked = self._exited_at.setdefault(session_id, now)
                if now - marked > REAP_AFTER_SECONDS:
                    del self._sessions[session_id]
                    self._exited_at.pop(session_id, None)
                    removed += 1
        return removed

    # ── discovery ───────────────────────────────────────────────────────────

    @staticmethod
    def harnesses(with_version: bool = False) -> list[dict[str, Any]]:
        return detect(with_version=with_version)

    @staticmethod
    def available_harness_ids() -> list[str]:
        return [h["id"] for h in detect() if h["installed"]]

    @staticmethod
    def relay_harness_ids() -> list[str]:
        return [s.id for s in SPECS.values() if s.transport == Transport.HTTP_STREAM]


#: Process-wide manager. The daemon, the CLI-in-process path and any embedding
#: service share ONE manager so a session is visible from every front-end.
_default: Optional[SessionManager] = None
_default_lock = threading.Lock()


def default_manager() -> SessionManager:
    global _default
    with _default_lock:
        if _default is None:
            _default = SessionManager()
        return _default

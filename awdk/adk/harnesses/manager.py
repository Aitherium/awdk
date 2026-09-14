"""The session manager — the actual "shell for shells".

Owns every live harness session on this host: creates them, routes turns to
them, fans their events out, and reaps them when they exit. One manager backs
all three front-ends (the AitherShell CLI, the AitherShell web app, and the
portal), so a session started on the desktop is the SAME session a phone
attaches to through the tunnel — not a copy.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from adk.harnesses.models import ModelBinding, ProfileError, resolve_binding
from adk.harnesses.registry import SPECS, HarnessSpec, Transport, detect, get
from adk.harnesses.session import HarnessSession, SessionConfig, SessionState

#: Sessions kept after exit so a client can still read the final transcript.
REAP_AFTER_SECONDS = float(os.environ.get("AITHER_HARNESS_REAP_SECONDS", "1800"))

#: Refuse to start more than this many concurrent sessions. Each is a real
#: coding agent with a model budget; an unbounded count is a runaway bill.
MAX_SESSIONS = int(os.environ.get("AITHER_HARNESS_MAX_SESSIONS", "24"))


class ManagerError(RuntimeError):
    """A session could not be created. Always surfaced — never defaulted away."""


class SessionManager:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._sessions: dict[str, HarnessSession] = {}
        self._lock = threading.RLock()
        self._root = root
        self._exited_at: dict[str, float] = {}

    # ── creation ────────────────────────────────────────────────────────────

    def create(self, config: SessionConfig, *, rows: int = 30, cols: int = 100) -> HarnessSession:
        try:
            spec: HarnessSpec = get(config.harness)
        except KeyError as exc:
            raise ManagerError(str(exc)) from exc

        with self._lock:
            live = [s for s in self._sessions.values() if s.state not in (
                SessionState.EXITED, SessionState.FAILED,
            )]
            if len(live) >= MAX_SESSIONS:
                raise ManagerError(
                    f"session limit reached ({MAX_SESSIONS} live). Close one first."
                )

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
                spec, config, binding, root=self._root, rows=rows, cols=cols
            )
        elif spec.id == "group":
            from adk.harnesses.agents import GroupSession

            participants = config.participants or ["aither"]
            session = GroupSession(
                spec, config, binding, root=self._root,
                participants=participants, base_url=config.base_url,
            )
        elif spec.transport == Transport.HTTP_STREAM:
            from adk.harnesses.agents import AgentRelaySession

            session = AgentRelaySession(
                spec, config, binding, root=self._root,
                agent=config.agent or "aither", base_url=config.base_url,
            )
        else:
            session = HarnessSession(spec, config, binding, root=self._root)
        with self._lock:
            self._sessions[session.id] = session
        session.start()
        return session

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

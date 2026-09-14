"""One live harness session: a process, an event stream, and a mailbox.

A session owns exactly one harness. It normalizes three very different process
shapes behind one interface:

- ``STRUCTURED_BIDI``  one long-lived process, JSON lines both ways (Claude Code)
- ``ONESHOT_PER_TURN`` a fresh process per turn (Gemini CLI, Codex, Aider)
- ``RAW_STREAM``       a long-lived process with byte stdio (a container shell)

Concurrency model
-----------------
Process I/O happens on plain threads, never on an event loop. The daemon is
async, and a blocking ``readline()`` inside a coroutine would stall every other
session on the same loop — a known failure mode when blocking primitives
enter the async event loop. Threads publish into a lock-guarded buffer;
async consumers poll that buffer. No blocking primitive ever touches the loop.

Durability
----------
Every event is appended to a JSONL transcript before it is fanned out, so a
session survives a client disconnect, a portal reload, or a phone dropping off
the tunnel. ``events_since(seq)`` replays from memory; the transcript is the
audit record and the thing Workforce/Strata can ingest later.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from adk.harnesses.events import EventKind, HarnessEvent, error, notice
from adk.harnesses.models import ModelBinding, apply_binding
from adk.harnesses.registry import HarnessSpec, LaunchSpec, Transport, resolve_binary

#: Events kept in memory for reconnecting clients. The transcript on disk is
#: complete; this only bounds RAM for a long-running session.
MAX_BUFFERED_EVENTS = 5000

#: Wall-clock ceiling on a ONESHOT_PER_TURN turn, in seconds.
#:
#: This exists because of a measured failure: an UNAUTHENTICATED Gemini CLI
#: hangs forever with no output and no exit. Without a ceiling the turn emitted
#: `turn.started` and then NOTHING — no text, no error, no completion — which
#: is indistinguishable from a model that is still thinking. The UI spins
#: forever and the operator debugs the wrong layer.
#:
#: A structured (bidi) harness does not need this: it streams events, so
#: silence there is already visible. A one-shot turn is one opaque process.
TURN_TIMEOUT_SECONDS = float(os.environ.get("AITHER_HARNESS_TURN_TIMEOUT", "600"))

#: How long a one-shot turn may produce NO output before we say so. Not a
#: failure — just the difference between "working" and "possibly wedged".
TURN_QUIET_NOTICE_SECONDS = float(os.environ.get("AITHER_HARNESS_TURN_QUIET", "45"))

#: Windows: never allocate a console for a child process. A scheduled or
#: background-spawned console window TAKES FOCUS on the logged-on desktop —
#: a known problem that CREATE_NO_WINDOW prevents.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class SessionState(str):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    EXITED = "exited"
    FAILED = "failed"


@dataclass
class SessionConfig:
    """Everything needed to create a session."""

    harness: str = "claude"
    cwd: str = ""
    model_profile: str = ""
    model: str = ""
    permission_mode: str = ""
    resume_session_id: str = ""
    system_prompt_append: str = ""
    add_dirs: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    mcp_config: str = ""
    target: str = ""
    extra_args: list[str] = field(default_factory=list)
    title: str = ""
    #: Free-form owner tag (portal user id, workforce agent id) for attribution.
    owner: str = ""
    #: Sovereign agent id for a relay session (``aither``, ``atlas``, ``lyra``…).
    agent: str = ""
    #: Agent ids sharing a group-chat room.
    participants: list[str] = field(default_factory=list)
    #: Genesis base URL override for relay/group sessions.
    base_url: str = ""
    #: Per-session ceiling on a one-shot turn, in seconds (0 = module default).
    turn_timeout: float = 0.0


class HarnessSession:
    """A live harness process plus its normalized event stream."""

    def __init__(
        self,
        spec: HarnessSpec,
        config: SessionConfig,
        binding: Optional[ModelBinding] = None,
        *,
        root: Optional[Path] = None,
        session_id: str = "",
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:16]
        self.spec = spec
        self.config = config
        self.binding = binding
        self.state: str = SessionState.STARTING
        self.created_at = time.time()
        self.exit_code: Optional[int] = None
        self.turn = 0
        #: The harness's OWN session id, learned from its init event. This is
        #: what --resume takes, and is not the same as self.id.
        self.harness_session_id = ""
        self.reported_model = ""

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._events: list[HarnessEvent] = []
        self._seq = 0
        self._threads: list[threading.Thread] = []
        self._closed = threading.Event()

        base = root or Path(
            os.environ.get("AITHER_HARNESS_ROOT", Path.home() / ".aither" / "harness-sessions")
        )
        self.dir = Path(base) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._transcript = self.dir / "events.jsonl"

    # ── event plumbing ──────────────────────────────────────────────────────

    def _emit(self, event: HarnessEvent) -> HarnessEvent:
        """Stamp, persist and buffer one event. Safe from any thread."""
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            event.session_id = self.id
            if not event.turn:
                event.turn = self.turn
            self._events.append(event)
            if len(self._events) > MAX_BUFFERED_EVENTS:
                del self._events[: len(self._events) - MAX_BUFFERED_EVENTS]
            payload = event.to_dict()
        # Transcript write is outside the lock: it is I/O, and a slow disk must
        # not serialize the reader threads. A failed append is reported inline
        # rather than swallowed — losing the audit record silently is worse
        # than a noisy session.
        try:
            with self._transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except OSError as exc:
            sys.stderr.write(f"[harness {self.id}] transcript write failed: {exc}\n")
        return event

    def events_since(self, seq: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            out = [e.to_dict() for e in self._events if e.seq > seq]
        return out[-limit:] if limit else out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # ── lifecycle ───────────────────────────────────────────────────────────

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.binding is not None:
            env = apply_binding(env, self.binding)
        # A child harness must never inherit our own stream-json wiring.
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        env["AITHER_HARNESS_SESSION"] = self.id
        return env

    def _launch_spec(self, prompt: str = "") -> LaunchSpec:
        return LaunchSpec(
            cwd=self.config.cwd,
            model=self.config.model,
            setting_sources=(
                self.binding.claude_setting_sources() if self.binding is not None else ""
            ),
            permission_mode=self.config.permission_mode,
            resume_session_id=self.config.resume_session_id or self.harness_session_id,
            system_prompt_append=self.config.system_prompt_append,
            add_dirs=list(self.config.add_dirs),
            allowed_tools=list(self.config.allowed_tools),
            mcp_config=self.config.mcp_config,
            prompt=prompt,
            target=self.config.target,
            extra_args=list(self.config.extra_args),
        )

    def start(self) -> None:
        """Start the session. Non-blocking: readers run on their own threads."""
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_STARTING,
                text=f"{self.spec.label}",
                data={
                    "harness": self.spec.id,
                    "transport": self.spec.transport.value,
                    "cwd": self.config.cwd,
                    "model_binding": self.binding.redacted() if self.binding else None,
                },
            )
        )

        if self.spec.transport == Transport.HTTP_STREAM:
            # Nothing to spawn — turns are relayed by the manager.
            self.state = SessionState.READY
            self._emit(
                HarnessEvent(kind=EventKind.SESSION_READY, text="relay", data={"relay": True})
            )
            return

        if self.spec.transport == Transport.ONESHOT_PER_TURN:
            # Nothing runs until the first turn arrives.
            self.state = SessionState.IDLE
            self._emit(
                HarnessEvent(
                    kind=EventKind.SESSION_READY,
                    text="idle (process starts per turn)",
                    data={"per_turn": True},
                )
            )
            return

        self._spawn_persistent()

    def _spawn_persistent(self) -> None:
        path = resolve_binary(self.spec)
        if not path:
            self.state = SessionState.FAILED
            self._emit(
                error(
                    f"{self.spec.label} is not installed on this host.",
                    harness=self.spec.id,
                    install_hint=self.spec.install_hint,
                )
            )
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            return

        argv = self.spec.argv(self._launch_spec())
        argv[0] = path
        cwd = self.config.cwd or os.getcwd()
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=self._child_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            self.state = SessionState.FAILED
            self._emit(error(f"failed to start {self.spec.label}: {exc}", argv=argv[:2]))
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            return

        self._start_reader(self._proc.stdout, stream="stdout")
        self._start_reader(self._proc.stderr, stream="stderr")
        self._start_waiter(self._proc)
        self.state = SessionState.READY

    def _start_reader(self, handle: Any, *, stream: str = "stdout") -> None:
        thread = threading.Thread(
            target=self._pump,
            args=(handle, stream),
            name=f"harness-{stream}-{self.id}",
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def _pump(self, handle: Any, stream: str) -> None:
        """Read one output stream to EOF, translating each line into events.

        Runs on a thread. Every line produces at least one event: an
        unclassifiable line becomes RAW rather than vanishing, because a
        silently dropped line is how a harness protocol change gets
        misdiagnosed as "the model stopped responding".
        """
        if handle is None:
            return
        try:
            for line in handle:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                if stream == "stderr":
                    # stderr is diagnostics, not model output. Surfaced as a
                    # notice so it is visible without polluting the transcript
                    # as assistant text.
                    self._emit(notice(line, stream="stderr"))
                    continue
                for event in self._translate_line(line):
                    self._observe(event)
                    self._emit(event)
        except (OSError, ValueError) as exc:
            self._emit(error(f"{stream} reader failed: {exc}"))
        finally:
            try:
                handle.close()
            except OSError as exc:
                # The stream is already gone. Nothing to recover, but say so —
                # a silently swallowed close is indistinguishable from a reader
                # that exited cleanly, and that difference matters when a
                # session ends for no visible reason.
                sys.stderr.write(
                    f"[harness {self.id}] {stream} close failed: {exc}\n"
                )

    def _translate_line(self, line: str) -> list[HarnessEvent]:
        if not self.spec.json_lines:
            return self.spec.translate(line)
        try:
            obj = json.loads(line)
        except ValueError:
            from adk.harnesses.events import raw as _raw

            return [_raw(line, reason="not json")]
        return self.spec.translate(obj)

    def _observe(self, event: HarnessEvent) -> None:
        """Learn session facts from outgoing events, and assert the model.

        The model assertion is the point: a per-session binding that silently
        did not take (because a global settings.json overrode it) would
        otherwise run an entire task on the wrong provider while the UI label
        says what you asked for.
        """
        if event.kind == EventKind.SESSION_READY:
            self.harness_session_id = str(event.data.get("harness_session_id") or "")
            self.reported_model = str(event.data.get("model") or "")
            self.state = SessionState.IDLE
            expected = self.binding.expected_model if self.binding else ""
            if expected and self.reported_model and self.reported_model != expected:
                self._emit(
                    notice(
                        f"model mismatch: asked for {expected!r}, harness reports "
                        f"{self.reported_model!r} — a global settings.json env block "
                        "may be overriding this session",
                        expected=expected,
                        reported=self.reported_model,
                        severity="warning",
                    )
                )
        elif event.kind == EventKind.TURN_COMPLETED:
            self.state = SessionState.IDLE
            harness_sid = str(event.data.get("harness_session_id") or "")
            if harness_sid:
                self.harness_session_id = harness_sid

    def _start_waiter(self, proc: subprocess.Popen) -> None:
        def wait() -> None:
            code = proc.wait()
            self.exit_code = code
            if self.state != SessionState.FAILED:
                self.state = SessionState.EXITED
            self._emit(
                HarnessEvent(
                    kind=EventKind.SESSION_EXITED,
                    text=f"exit {code}",
                    data={"exit_code": code},
                )
            )
            self._closed.set()

        thread = threading.Thread(target=wait, name=f"harness-wait-{self.id}", daemon=True)
        thread.start()
        self._threads.append(thread)

    # ── turns ───────────────────────────────────────────────────────────────

    def send(self, text: str) -> bool:
        """Deliver one user turn. Returns False when the session cannot take it."""
        if self.spec.transport == Transport.ONESHOT_PER_TURN:
            return self._send_oneshot(text)
        if self.spec.transport == Transport.HTTP_STREAM:
            self._emit(error("relay harness turns are delivered by the manager"))
            return False

        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._emit(error("session is not running"))
            return False

        with self._lock:
            self.turn += 1
            turn = self.turn
        self._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text=text, turn=turn))
        self.state = SessionState.BUSY

        # PTY sessions never reach here — PtyHarnessSession.send() owns raw
        # keystroke delivery. Everything that DOES reach here is a structured
        # harness and must encode its turn.
        encode = self.spec.encode_input
        if encode is None:
            self._emit(error(f"harness '{self.spec.id}' cannot encode input"))
            return False
        line = encode(text) + "\n"

        try:
            assert proc.stdin is not None
            proc.stdin.write(line)
            proc.stdin.flush()
        except (OSError, ValueError, AssertionError) as exc:
            self._emit(error(f"write to harness failed: {exc}"))
            return False
        return True

    def _send_oneshot(self, text: str) -> bool:
        with self._lock:
            self.turn += 1
            turn = self.turn
        self._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text=text, turn=turn))

        path = resolve_binary(self.spec)
        if not path:
            self._emit(
                error(
                    f"{self.spec.label} is not installed on this host.",
                    install_hint=self.spec.install_hint,
                )
            )
            return False

        argv = self.spec.argv(self._launch_spec(prompt=text))
        argv[0] = path
        self.state = SessionState.BUSY
        seq_at_start = self.last_seq

        def run() -> None:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.config.cwd or os.getcwd(),
                    env=self._child_env(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=_NO_WINDOW,
                )
            except (OSError, ValueError) as exc:
                self._emit(error(f"failed to start {self.spec.label}: {exc}"))
                self.state = SessionState.IDLE
                return
            self._proc = proc
            out = threading.Thread(
                target=self._pump, args=(proc.stdout, "stdout"), daemon=True
            )
            err = threading.Thread(
                target=self._pump, args=(proc.stderr, "stderr"), daemon=True
            )
            out.start()
            err.start()

            timeout = self.config.turn_timeout or TURN_TIMEOUT_SECONDS
            started = time.time()
            timed_out = False
            quiet_warned = False
            code: Optional[int] = None
            while True:
                try:
                    code = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.time() - started
                    if not quiet_warned and elapsed >= TURN_QUIET_NOTICE_SECONDS:
                        # Only say "quiet" if it really has produced nothing.
                        if self.last_seq <= seq_at_start:
                            quiet_warned = True
                            self._emit(
                                notice(
                                    f"{self.spec.label} has produced no output in "
                                    f"{int(elapsed)}s",
                                    quiet_seconds=int(elapsed),
                                )
                            )
                    if elapsed >= timeout:
                        timed_out = True
                        proc.kill()
                        code = proc.wait(timeout=10)
                        break

            out.join(timeout=5)
            err.join(timeout=5)
            if timed_out:
                # Loud, and it names the likely cause. A one-shot harness that
                # hangs with no output is almost always waiting on auth.
                self._emit(
                    error(
                        f"{self.spec.label} turn exceeded {int(timeout)}s and was killed. "
                        "A one-shot harness that produces no output is usually "
                        "unauthenticated — run it once interactively to sign in.",
                        timeout_seconds=int(timeout),
                        harness=self.spec.id,
                    )
                )
            self._emit(
                HarnessEvent(
                    kind=EventKind.TURN_COMPLETED,
                    text="",
                    data={
                        "is_error": timed_out or code != 0,
                        "exit_code": code,
                        "timed_out": timed_out,
                    },
                    turn=turn,
                )
            )
            self.state = SessionState.IDLE

        thread = threading.Thread(target=run, name=f"harness-turn-{self.id}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return True

    def interrupt(self) -> bool:
        """Cancel the in-flight turn without killing a persistent session."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        if self.spec.transport == Transport.ONESHOT_PER_TURN:
            proc.terminate()
            self._emit(notice("turn interrupted"))
            return True
        # Structured harnesses have no interrupt channel on stdin; the honest
        # answer is that we cannot, rather than pretending it worked.
        self._emit(notice("interrupt not supported by this harness transport"))
        return False

    def stop(self, timeout: float = 8.0) -> None:
        """Stop the session and do not return until the exit is RECORDED.

        Setting the closed flag here rather than waiting for the waiter thread
        would let a caller observe ``exit_code is None`` and no SESSION_EXITED
        event on a session that had in fact exited cleanly — a stop that
        reports less than it achieved.
        """
        proc = self._proc
        if proc is None:
            self.state = SessionState.EXITED
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            self._closed.set()
            return
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (OSError, ValueError) as exc:
                sys.stderr.write(f"[harness {self.id}] stdin close failed: {exc}\n")
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
        # The waiter thread owns exit_code and the SESSION_EXITED event; give it
        # a bounded moment to publish before we declare the session closed.
        if not self._closed.wait(timeout=min(timeout, 5.0)):
            self.state = SessionState.EXITED
            self.exit_code = proc.returncode
            self._emit(
                HarnessEvent(
                    kind=EventKind.SESSION_EXITED,
                    text=f"exit {proc.returncode}",
                    data={"exit_code": proc.returncode, "note": "recorded by stop()"},
                )
            )
            self._closed.set()

    def wait_closed(self, timeout: Optional[float] = None) -> bool:
        return self._closed.wait(timeout)

    # ── introspection ───────────────────────────────────────────────────────

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "harness": self.spec.id,
            "harness_label": self.spec.label,
            "transport": self.spec.transport.value,
            "state": self.state,
            "title": self.config.title or self.spec.label,
            "cwd": self.config.cwd,
            "owner": self.config.owner,
            "created_at": self.created_at,
            "turn": self.turn,
            "last_seq": self.last_seq,
            "exit_code": self.exit_code,
            "harness_session_id": self.harness_session_id,
            "reported_model": self.reported_model,
            "model_binding": self.binding.redacted() if self.binding else None,
            "transcript": str(self._transcript),
        }

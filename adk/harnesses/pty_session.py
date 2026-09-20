"""Real PTY sessions — an actual terminal, not a fake one.

The terminal app shipped in the Living OS desktop today
(``AitherVeil/src/components/os/apps/terminal.tsx``) is a 142-line ``switch``
over hardcoded strings; only its ``node`` command touches the network. This
module is the replacement substrate: a genuine pseudo-terminal with a real
shell behind it, job control, colours, curses apps, the lot.

Two flavours, one implementation
--------------------------------
- **local**   a shell on this host (pwsh/bash), for driving the machine.
- **sandbox** ``docker exec -it <container> /bin/bash`` — a real Linux TTY
  inside a dev-workspace container. The container is allocated a genuine PTY by
  Docker, so ``top``, ``vim`` and ``less`` behave. This is what makes "linux in
  a terminal" true rather than a shell-shaped chat box.

Both are the same class with different argv, because the only thing that
differs is what is on the other end of the pty.

Platform reality
----------------
``pty`` is POSIX-only; Windows needs ConPTY via ``pywinpty``. Both are wrapped
behind :class:`_PtyBackend`. If neither is importable the session FAILS LOUDLY
at start with an install hint — it never silently degrades to a pipe, because a
pipe-backed "terminal" is precisely the fake terminal this replaces: it looks
right until you run something interactive.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Optional

from adk.harnesses.events import EventKind, HarnessEvent, error, notice
from adk.harnesses.models import MANAGED_VARS
from adk.harnesses.registry import resolve_binary
from adk.harnesses.session import HarnessSession, SessionState

#: How much to read per pty read() call.
_READ_SIZE = 65536

#: Pause between typing a submitted line and pressing Enter, so a TUI's paste detection
#: sees two events (text, then a key) rather than one burst. See ``submit``.
SUBMIT_ENTER_DELAY_S = float(os.environ.get("AITHER_PTY_SUBMIT_ENTER_DELAY", "0.15"))


class PtyUnavailableError(RuntimeError):
    """No pty backend on this platform. Raised loudly, never worked around."""


class _PtyBackend:
    """Thin uniform wrapper over pywinpty (Windows) and ptyprocess/pty (POSIX)."""

    def __init__(self, proc: Any, kind: str) -> None:
        self._proc = proc
        self.kind = kind

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        rows: int,
        cols: int,
    ) -> "_PtyBackend":
        if sys.platform == "win32":
            try:
                from winpty import PtyProcess  # type: ignore[import-not-found]
            except ImportError as exc:
                raise PtyUnavailableError(
                    "pywinpty is required for terminal sessions on Windows: "
                    "pip install pywinpty"
                ) from exc
            proc = PtyProcess.spawn(
                argv, cwd=cwd or None, env=env, dimensions=(rows, cols)
            )
            return cls(proc, "winpty")

        try:
            from ptyprocess import PtyProcessUnicode  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PtyUnavailableError(
                "ptyprocess is required for terminal sessions: pip install ptyprocess"
            ) from exc
        proc = PtyProcessUnicode.spawn(
            argv, cwd=cwd or None, env=env, dimensions=(rows, cols)
        )
        return cls(proc, "ptyprocess")

    def read(self, size: int = _READ_SIZE) -> str:
        return self._proc.read(size)

    def write(self, data: str) -> int:
        return self._proc.write(data)

    def isalive(self) -> bool:
        return bool(self._proc.isalive())

    def setwinsize(self, rows: int, cols: int) -> None:
        setter = getattr(self._proc, "setwinsize", None)
        if setter is not None:
            setter(rows, cols)

    def terminate(self, force: bool = False) -> None:
        self._proc.terminate(force)

    @property
    def exitstatus(self) -> Optional[int]:
        return getattr(self._proc, "exitstatus", None)


def default_shell_argv() -> list[str]:
    """The best interactive shell available on this host."""
    if sys.platform == "win32":
        for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
            found = shutil.which(candidate)
            if found:
                return [found]
        return ["cmd.exe"]
    for candidate in (os.environ.get("SHELL", ""), "bash", "sh"):
        if candidate:
            found = shutil.which(candidate)
            if found:
                return [found, "-l"] if candidate != "sh" else [found]
    return ["/bin/sh"]


class PtyHarnessSession(HarnessSession):
    """A :class:`HarnessSession` whose child is behind a real pseudo-terminal.

    Output is emitted as ``TEXT_DELTA`` events carrying raw bytes-as-text with
    ANSI sequences INTACT. xterm.js on the front-end renders them directly; a
    consumer that wants plain text can strip escapes itself. Stripping here
    would throw away exactly the information that makes a terminal a terminal.
    """

    def __init__(self, *args: Any, rows: int = 30, cols: int = 100, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rows = rows
        self.cols = cols
        self._pty: Optional[_PtyBackend] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_STARTING,
                text=self.spec.label,
                data={
                    "harness": self.spec.id,
                    "transport": self.spec.transport.value,
                    "cwd": self.config.cwd,
                    "rows": self.rows,
                    "cols": self.cols,
                },
            )
        )
        argv = self._resolve_argv()
        if argv is None:
            return

        env = self._scrub_program_env(self._child_env())
        # A pty session IS a terminal; advertise one so curses apps behave.
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = str(self.cols)
        env["LINES"] = str(self.rows)

        try:
            self._pty = _PtyBackend.spawn(
                argv,
                cwd=self.config.cwd or os.getcwd(),
                env=env,
                rows=self.rows,
                cols=self.cols,
            )
        except PtyUnavailableError as exc:
            self.state = SessionState.FAILED
            self._emit(error(str(exc), install_hint="pip install pywinpty ptyprocess"))
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            return
        except (OSError, ValueError) as exc:
            self.state = SessionState.FAILED
            self._emit(error(f"failed to open terminal: {exc}", argv=argv[:3]))
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            return

        self.state = SessionState.READY
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=" ".join(argv[:3]),
                data={
                    "pty": self._pty.kind,
                    "argv": argv,
                    "rows": self.rows,
                    "cols": self.cols,
                },
            )
        )
        thread = threading.Thread(target=self._pump_pty, name=f"pty-{self.id}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _scrub_program_env(self, env: dict[str, str]) -> dict[str, str]:
        """A PROGRAM harness (claude-tty) never inherits the daemon's model wiring.

        ``_child_env`` copies ``os.environ`` verbatim. The daemon may itself be running
        under a bridge profile (ANTHROPIC_BASE_URL at a fleet model); a shell session
        inheriting that is the owner's business, but a Claude Code tab spawned here
        would silently run its brain on a 32k window that cannot hold the repo's
        always-on prompt. Scrubbed for the harnesses that declare no model binding
        AND build their own argv -- i.e. the program harnesses, not the shells.
        """
        if self.spec.build_argv is None or self.spec.supports_model_binding:
            return env
        for var in MANAGED_VARS:
            env.pop(var, None)
        return env

    def _resolve_argv(self) -> Optional[list[str]]:
        """Build argv for this terminal flavour, or fail loudly."""
        if self.spec.id == "sandbox":
            container = (self.config.target or "").strip()
            if not container:
                self.state = SessionState.FAILED
                self._emit(error("sandbox terminal requires a target container"))
                self._emit(
                    HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None})
                )
                return None
            docker = shutil.which("docker")
            if not docker:
                self.state = SessionState.FAILED
                self._emit(error("docker is not installed on this host"))
                self._emit(
                    HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None})
                )
                return None
            shell = self.config.extra_args[0] if self.config.extra_args else "/bin/bash"
            return [docker, "exec", "-it", container, shell]

        if self.spec.build_argv is not None:
            # A pty harness that is a PROGRAM (claude-tty), not a shell: the spec builds
            # argv exactly as the structured harnesses do, and argv[0] goes through
            # resolve_binary so the npm .cmd shim becomes the real .exe -- ConPTY then
            # runs the program itself, and an interrupt reaches it, not a cmd.exe wrapper.
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
                self._emit(
                    HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None})
                )
                return None
            launch = self._launch_spec()
            if launch.resume_session_id:
                # Resuming: the program's id IS the one we were handed.
                self.harness_session_id = launch.resume_session_id
            elif self.spec.supports_resume:
                # ONE id for one Claude Code. Minted here, handed to the program as
                # --session-id, so the daemon row and the transcript the program writes
                # under its own state dir share it and the session directory folds them
                # (session_directory.py) instead of listing the same tab twice with two
                # steer capabilities.
                self.harness_session_id = str(uuid.uuid4())
                launch.session_id = self.harness_session_id
            launch.title = self.config.title
            argv = self.spec.argv(launch)
            argv[0] = path
            return argv

        if self.config.extra_args:
            return list(self.config.extra_args)
        return default_shell_argv()

    # ── I/O ─────────────────────────────────────────────────────────────────

    def _pump_pty(self) -> None:
        pty = self._pty
        if pty is None:
            return
        while True:
            try:
                chunk = pty.read(_READ_SIZE)
            except EOFError:
                break
            except (OSError, ValueError) as exc:
                self._emit(error(f"terminal read failed: {exc}"))
                break
            if not chunk:
                if not pty.isalive():
                    break
                time.sleep(0.02)
                continue
            self._emit(HarnessEvent(kind=EventKind.TEXT_DELTA, text=chunk))
        code = pty.exitstatus
        self.exit_code = code
        self.state = SessionState.EXITED
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_EXITED, text=f"exit {code}", data={"exit_code": code}
            )
        )
        self._closed.set()

    def send(self, text: str) -> bool:
        """Write raw input to the terminal. No newline is added.

        A terminal takes KEYSTROKES, not messages: the caller decides whether a
        payload ends in ``\\r``. Appending one here would make it impossible to
        send a bare Ctrl-C or answer a single-key prompt.
        """
        pty = self._pty
        if pty is None or not pty.isalive():
            self._emit(error("terminal is not running"))
            return False
        try:
            pty.write(text)
        except (OSError, ValueError) as exc:
            self._emit(error(f"terminal write failed: {exc}"))
            return False
        return True

    def submit(self, text: str) -> bool:
        """Type ``text`` as ONE line and press Enter.

        ``send`` stays raw (a bare Ctrl-C, a single-key answer, the awsh pty attach
        forwarding keystrokes verbatim all depend on it). This is the message-shaped
        sibling a dispatcher wants: newline runs collapse to one space because the one
        shape known to submit in Claude Code's input box is a single line ending in
        ``\r`` -- what a multi-line paste does there is unmeasured, and a steer that
        half-submits is worse than one that arrives as a long line. Whitespace-only
        text is refused (there is nothing to submit) rather than sending a bare Enter,
        which would answer whatever prompt happened to be open.
        """
        line = " ".join(part.strip() for part in text.splitlines() if part.strip())
        if not line:
            return False
        # The Enter is its OWN keystroke, after a beat. Measured 2026-09-19 against
        # Claude Code 2.1.278: ``line + "\r"`` in ONE write submitted a 50-character
        # prompt and left an 83-character one sitting in the input box forever -- a burst
        # that size is read as a PASTE, and the trailing CR becomes part of the pasted
        # text instead of the key that submits it. The receipt said "the agent has it
        # now" over an unsubmitted line, which is the exact lie submit() exists to end.
        if not self.send(line):
            return False
        time.sleep(SUBMIT_ENTER_DELAY_S)
        return self.send("\r")

    def resize(self, rows: int, cols: int) -> bool:
        pty = self._pty
        if pty is None:
            return False
        self.rows, self.cols = max(1, rows), max(1, cols)
        try:
            pty.setwinsize(self.rows, self.cols)
        except (OSError, ValueError) as exc:
            self._emit(notice(f"resize failed: {exc}"))
            return False
        return True

    def interrupt(self) -> bool:
        """Ctrl-C into the terminal — the real thing, not a process kill."""
        return self.send("\x03")

    def stop(self, timeout: float = 8.0) -> None:
        pty = self._pty
        if pty is None:
            self.state = SessionState.EXITED
            self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": None}))
            self._closed.set()
            return
        if pty.isalive():
            try:
                pty.terminate(True)
            except (OSError, ValueError) as exc:
                sys.stderr.write(f"[pty {self.id}] terminate failed: {exc}\n")
        if not self._closed.wait(timeout=min(timeout, 5.0)):
            self.state = SessionState.EXITED
            self._emit(
                HarnessEvent(
                    kind=EventKind.SESSION_EXITED,
                    data={"exit_code": pty.exitstatus, "note": "recorded by stop()"},
                )
            )
            self._closed.set()

    def info(self) -> dict[str, Any]:
        data = super().info()
        data.update({"rows": self.rows, "cols": self.cols, "pty": True})
        return data


def pty_available() -> tuple[bool, str]:
    """(available, detail) — used by ``detect`` so the UI never offers a lie."""
    if sys.platform == "win32":
        try:
            import winpty  # noqa: F401
        except ImportError:
            return (False, "pip install pywinpty")
        return (True, "conpty")
    try:
        import ptyprocess  # noqa: F401
    except ImportError:
        return (False, "pip install ptyprocess")
    return (True, "posix pty")

"""Two-way bridge between a terminal and a daemon-owned harness session.

This module implements the `adk harness wrap` mechanism: a thin read-eval loop
that bridges stdin/stdout/stderr from a terminal to a daemon session.

The bridge:
  - Creates a daemon session (via POST /sessions)
  - Reads user input from terminal stdin on a thread
  - Polls the daemon's SSE stream (GET /sessions/{id}/stream)
  - Writes events to stdout with minimal formatting (preserves ANSI)
  - Handles Ctrl-C by sending interrupt (POST /sessions/{id}/interrupt)

The terminal remains the client; the daemon is the owner. The session survives
terminal disconnection and is visible to other clients (browser, phone tunnel).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from adk.harnesses.daemon import DEFAULT_HOST, DEFAULT_PORT, resolve_token

#: Event kinds a terminal deliberately does NOT render — lifecycle and accounting
#: signals that belong to the cockpit, not to a shell the human is reading. They
#: are enumerated rather than left to a fall-through so that "we chose not to show
#: this" and "we have never heard of this" stay distinguishable.
_QUIET_KINDS = frozenset({
    "session.starting",
    "session.ready",
    "turn.started",
    "usage",
    "notice",
    "raw",
})


class DaemonPtyBridge:
    """Two-way terminal <-> daemon session bridge."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
    ) -> None:
        """Initialize the bridge.

        Args:
            host: Daemon host (default: 127.0.0.1)
            port: Daemon port (default: 8362)
            token: Bearer token (resolved if empty)
        """
        self.host = host or DEFAULT_HOST
        self.port = port or DEFAULT_PORT
        self.token = resolve_token(token)
        self.session_id: str = ""
        self.last_seq: int = 0
        self._stdin_thread: Optional[threading.Thread] = None
        self._input_queue: list[str] = []
        self._input_lock = threading.Lock()
        self._closed = threading.Event()

    def _base_url(self) -> str:
        """Build the daemon base URL."""
        return f"http://{self.host}:{self.port}"

    def _request(
        self,
        path: str,
        method: str = "GET",
        body: Optional[dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> tuple[int, Any]:
        """Make an authenticated request to the daemon.

        Returns (status_code, response_body). On error, status=0 or >=400.
        """
        url = self._base_url() + path
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, f"cannot reach daemon at {url}: {exc.reason}"

    def _stream_events(self, since: int = 0) -> None:
        """Poll /sessions/{id}/stream (SSE) and write events to stdout.

        Loops until the daemon reports session.exited or a network error.
        SSE format: event: <kind>\ndata: <json>\n\n
        """
        path = f"/sessions/{self.session_id}/stream?since={since}"
        url = self._base_url() + path
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(req, timeout=60.0) as response:
                kind = ""
                data_str = ""
                for line in response:
                    line_str = line.decode("utf-8", "replace").rstrip("\n\r")
                    if not line_str:
                        # Blank line = event boundary. Emit if we have a kind.
                        if kind and data_str:
                            try:
                                event = json.loads(data_str)
                                self.last_seq = event.get("seq", self.last_seq)
                                self._render(event, kind)
                                if kind == "session.exited":
                                    return
                            except (json.JSONDecodeError, ValueError) as exc:
                                # A malformed event used to be discarded in
                                # silence, so a daemon emitting broken JSON looked
                                # exactly like a session that had simply stopped
                                # talking. Say it on stderr — stdout stays clean
                                # for the session's own output.
                                sys.stderr.write(
                                    f"\n\033[33m! dropped a malformed '{kind}' event: "
                                    f"{exc}\033[0m\n"
                                )
                                sys.stderr.flush()
                        kind = ""
                        data_str = ""
                        continue
                    if line_str.startswith(":"):
                        # Comment (keepalive) — skip
                        continue
                    if line_str.startswith("event: "):
                        kind = line_str[7:].strip()
                    elif line_str.startswith("data: "):
                        data_str = line_str[6:]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # 404 = session not found (closed)
                sys.stderr.write(f"stream error: HTTP {exc.code}\n")
        except urllib.error.URLError as exc:
            sys.stderr.write(f"stream error: {exc.reason}\n")
        except Exception as exc:
            sys.stderr.write(f"stream error: {exc}\n")

    def _render(self, event: dict[str, Any], kind: str) -> None:
        """Render an event to stdout. Preserves ANSI sequences.

        Every member of ``EventKind`` is either rendered here or listed in
        ``_QUIET_KINDS``; anything else is reported as unhandled rather than
        dropped. ``test_wrap_event_coverage`` asserts that partition against the
        live enum, so adding a kind to the daemon fails the test instead of
        silently producing a terminal that shows nothing.
        """
        data = event.get("data", {})
        who = data.get("participant") or ""
        prefix = f"[{who}] " if who else ""

        if kind == "text.delta":
            text = event.get("text", "")
            sys.stdout.write(prefix + text)
            sys.stdout.flush()
        elif kind == "thinking.delta":
            text = event.get("text", "")
            sys.stdout.write(f"\033[2m{text}\033[0m")
            sys.stdout.flush()
        elif kind == "tool.call":
            tool = event.get("tool", "?")
            sys.stdout.write(f"\n\033[36m→ {tool}\033[0m")
            sys.stdout.flush()
        elif kind == "tool.result":
            tool = event.get("tool", "tool")
            is_error = data.get("is_error", False)
            state = "error" if is_error else "ok"
            sys.stdout.write(f"\033[36m← {tool} ({state})\033[0m")
            sys.stdout.flush()
        elif kind == "error":
            text = event.get("text", "?")
            sys.stderr.write(f"\n\033[31m! {text}\033[0m\n")
            sys.stderr.flush()
        elif kind == "turn.completed":
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif kind == "session.exited":
            code = data.get("exit_code")
            sys.stderr.write(f"\n\033[2m-- session exited ({code}) --\033[0m\n")
            sys.stderr.flush()
        elif kind in _QUIET_KINDS:
            # Deliberately not rendered: lifecycle and accounting events that
            # would be noise in a terminal the human is reading as a shell.
            pass
        else:
            # An UNKNOWN kind is different from a quiet one, and the difference
            # matters: the daemon's EventKind enum can grow, and the previous
            # version's single fall-through meant a new kind was discarded with
            # no output and no error — indistinguishable from the session having
            # nothing to say. Naming it is what makes the gap visible instead of
            # looking like silence.
            sys.stderr.write(f"\033[2m[unhandled event: {kind}]\033[0m\n")
            sys.stderr.flush()

    def _read_stdin(self) -> None:
        """Read lines from stdin on a thread and queue them for sending.

        Runs in the background; the main thread polls the queue and sends.
        """
        try:
            while not self._closed.is_set():
                try:
                    line = sys.stdin.readline()
                    if not line:
                        # EOF
                        break
                    with self._input_lock:
                        self._input_queue.append(line)
                except Exception:
                    break
        except KeyboardInterrupt:
            # Signalled on the stdin thread. The main thread's handler owns the
            # interrupt, so there is genuinely nothing to do here — but the
            # `finally` below is what closes the bridge, and marking that
            # explicitly keeps this from reading as a swallowed error.
            self._closed.set()
        finally:
            self._closed.set()

    def run(
        self,
        harness: str = "claude",
        cwd: str = "",
        model: str = "",
        resume_session_id: str = "",
        title: str = "",
    ) -> int:
        """Create a daemon session and run the two-way bridge.

        Args:
            harness: Harness type (default: claude)
            cwd: Working directory (default: current)
            model: Model profile or id (default: none)
            resume_session_id: Resume a previous session (default: none)
            title: Session title for the daemon

        Returns:
            Exit code (0 on clean exit, non-zero on error)
        """
        # Resolve cwd
        if not cwd:
            cwd = os.getcwd()
        cwd = os.path.abspath(cwd)

        # Create the session
        body = {
            "harness": harness,
            "cwd": cwd,
            "model": model,
            "resume_session_id": resume_session_id,
            "title": title or f"wrapped-{harness}",
        }
        status, payload = self._request("/sessions", "POST", body)
        if status != 200:
            sys.stderr.write(
                f"failed to create session: HTTP {status}: {payload}\n"
            )
            return 1
        self.session_id = payload.get("id", "")
        if not self.session_id:
            sys.stderr.write("daemon did not return a session id\n")
            return 1

        # Set up Ctrl-C handler
        def sigint_handler(sig: int, frame: Any) -> None:
            # Send interrupt to the daemon (fire-and-forget)
            try:
                status, _ = self._request(
                    f"/sessions/{self.session_id}/interrupt", "POST"
                )
                if status >= 400:
                    sys.stderr.write(
                        f"\n\033[33m! interrupt not accepted (HTTP {status}) — the "
                        f"session may still be running\033[0m\n"
                    )
            except Exception as exc:  # noqa: BLE001 - a signal handler must not raise
                # Swallowing this made Ctrl-C look like it worked when the
                # interrupt never reached the daemon: the local bridge closed,
                # the terminal returned, and the session kept running on the
                # host with nothing said. Say it instead.
                sys.stderr.write(
                    f"\n\033[33m! could not interrupt the session: {exc}\033[0m\n"
                )
            self._closed.set()

        original_sigint = signal.signal(signal.SIGINT, sigint_handler)

        try:
            # Start background threads
            self._stdin_thread = threading.Thread(
                target=self._read_stdin, daemon=True, name="wrap-stdin-reader"
            )
            self._stdin_thread.start()

            stream_thread = threading.Thread(
                target=self._stream_events, daemon=True, name="wrap-stream-reader"
            )
            stream_thread.start()

            # Main loop: poll for queued input and send it
            # Continues until stream closes (session.exited) or error
            while not self._closed.is_set():
                with self._input_lock:
                    if self._input_queue:
                        text = self._input_queue.pop(0)
                        status, payload = self._request(
                            f"/sessions/{self.session_id}/input",
                            "POST",
                            {"text": text},
                        )
                        if status >= 400:
                            # Session may have exited; let stream detect it
                            pass
                time.sleep(0.05)  # Small poll interval to avoid busy-waiting

            # Wait for stream thread to finish (should exit quickly after
            # session.exited is seen)
            stream_thread.join(timeout=2.0)
            return 0
        except KeyboardInterrupt:
            # User pressed Ctrl-C outside the signal handler
            self._closed.set()
            return 0
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            self._closed.set()

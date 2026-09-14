"""
AitherShell Interactive REPL
=============================

Interactive shell environment with:
- Non-blocking input: user can ALWAYS type, even during generation
- Steering: input during generation is injected into the active session
- Multi-line input support (end with "...")
- Command history saved to ~/.aither/history
- Graceful Ctrl+C handling
- Response streaming
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional, List
from uuid import uuid4

from adk.shell.config import AitherConfig, CONFIG_DIR
from adk.shell.genesis_client import GenesisClient, GenesisError

logger = logging.getLogger(__name__)


class AitherREPL:
    """
    Interactive REPL for AitherShell.

    The REPL is NEVER blocked — input reading runs concurrently with
    response streaming.  When a generation is active, typed input is
    sent as a steering message to the running session via /chat/steer.
    When idle, typed input starts a new request.
    """

    def __init__(self, config: AitherConfig):
        if not config:
            raise ValueError("config cannot be None")

        self.config = config
        self.genesis_client = GenesisClient(
            base_url=config.url,
            timeout=30.0,
            enable_logging=True,
        )
        self.history: List[str] = []
        self.history_file = Path(config.history_file)
        self._load_history()

        # Concurrency state
        self._generating = False       # True while a response is streaming
        self._active_session: Optional[str] = None  # session_id of active generation
        self._input_queue: asyncio.Queue = asyncio.Queue()  # pending user inputs
        self._shutdown = False
        self._thinking_active = False  # True while streaming think tokens
        self._tokens_displayed = False  # True when token events have streamed content

        # Artifact tracking — persists across generations within session
        self._artifacts: List[dict] = []

    def _load_history(self) -> None:
        try:
            if self.history_file.exists():
                lines = self.history_file.read_text(encoding="utf-8").splitlines()
                self.history = lines[-self.config.max_history :]
        except Exception as e:
            logger.warning(f"Failed to load history: {e}")

    def _save_history(self) -> None:
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            lines = self.history[-self.config.max_history :]
            self.history_file.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save history: {e}")

    # ── Input reader (runs forever in background) ────────────────────

    def _read_line(self, prompt: str) -> str:
        """Blocking input() wrapper for run_in_executor."""
        return input(prompt)

    async def _input_loop(self) -> None:
        """Continuously read user input and push to _input_queue.

        Runs in a background task so the event loop is never blocked.
        During generation, waits for output to finish before showing
        the next prompt — but the user can still Ctrl+C to cancel.
        """
        loop = asyncio.get_event_loop()
        while not self._shutdown:
            # Wait for active generation to finish before showing prompt.
            # This prevents prompt text from interleaving with streamed
            # response output. Ctrl+C still works during this wait.
            while self._generating and not self._shutdown:
                await asyncio.sleep(0.1)

            if self._shutdown:
                break

            try:
                line = await loop.run_in_executor(
                    None, self._read_line, self.config.prompt,
                )
            except EOFError:
                self._shutdown = True
                await self._input_queue.put(None)  # sentinel
                return
            except KeyboardInterrupt:
                if self._generating and self._active_session:
                    # Ctrl+C during generation → cancel the session
                    print("\n^C — cancelling active session...")
                    await self.genesis_client.steer(
                        self._active_session, "cancel", action="cancel",
                    )
                else:
                    print("\n^C — type /exit to quit")
                continue

            if not line:
                continue

            # Multi-line support: end with "..." to continue
            lines = [line]
            while line.endswith("..."):
                lines[-1] = lines[-1][:-3]
                try:
                    line = await loop.run_in_executor(None, input, "... ")
                    lines.append(line)
                except (EOFError, KeyboardInterrupt):
                    break

            text = " ".join(lines).strip()
            if text:
                await self._input_queue.put(text)

    # ── Command handling ─────────────────────────────────────────────

    async def _handle_command(self, cmd: str) -> Optional[str]:
        cmd = cmd.lstrip("/").strip()
        parts = cmd.split(None, 1)
        command = parts[0] if parts else ""

        if command == "help":
            return (
                "\nAitherShell Built-in Commands:\n\n"
                "/help                  Show this help\n"
                "/history               Show command history\n"
                "/artifacts             List all artifacts from this session\n"
                "/get N [path]          Download artifact #N (default: current dir)\n"
                "/clear                 Clear history\n"
                "/new                   Start a fresh session\n"
                "/sessions              List recent sessions\n"
                "/resume <session_id>   Resume a previous session\n"
                "/research <question>   Research a question (web + report)\n"
                "/exit, /quit           Exit shell\n\n"
                "During generation, type anything to steer the active session.\n"
                "Ctrl+C during generation cancels it.\n"
            )
        elif command == "history":
            if not self.history:
                return "History is empty."
            lines = [f"{i+1:3d} {c}" for i, c in enumerate(self.history[-20:])]
            return "\n".join(lines)
        elif command == "artifacts":
            return self._format_artifacts()
        elif command == "get":
            arg = parts[1].strip() if len(parts) > 1 else ""
            await self._download_artifact(arg)
            return None  # output handled internally
        elif command == "clear":
            self.history.clear()
            self._save_history()
            return "History cleared."
        elif command == "resume":
            if len(parts) < 2 or not parts[1].strip():
                return "Usage: /resume <session_id>"
            session_id = parts[1].strip()
            self.config.session_id = session_id
            self.config.last_session_id = session_id
            # Try to save config
            try:
                from adk.shell.config import save_config
                save_config(self.config)
            except Exception:
                pass
            return f"Resumed session {session_id}"
        elif command == "new":
            old_id = self.config.session_id or "(none)"
            self.config.session_id = None
            self.config.last_session_id = None
            try:
                from adk.shell.config import save_config
                save_config(self.config)
            except Exception:
                pass
            return f"Started fresh session (previous: {old_id})"
        elif command == "sessions":
            return self._list_sessions()
        elif command == "research":
            if len(parts) < 2 or not parts[1].strip():
                return "Usage: /research <question>"
            question = parts[1].strip()
            # Frame as research prompt
            research_prompt = (
                "Research the following thoroughly using web sources and deliver "
                "a written report with citations:\n\n"
                f"{question}"
            )
            # Queue the research prompt for processing
            await self._input_queue.put(research_prompt)
            return None  # Handled via queue
        elif command in ("exit", "quit"):
            self._shutdown = True
            return None
        else:
            return f"Unknown command: /{command}. Type /help for help."

    def _format_artifacts(self) -> str:
        """Format the artifact list for display."""
        if not self._artifacts:
            return "No artifacts in this session."
        lines = [f"\n  Artifacts ({len(self._artifacts)} total):\n"]
        for i, art in enumerate(self._artifacts, 1):
            name = art.get("name") or art.get("filename") or art.get("path", "unknown")
            atype = art.get("artifact_type") or art.get("type", "file")
            size = art.get("size_bytes", 0)
            size_str = f"  {size:,} bytes" if size else ""
            lines.append(f"  #{i:2d}  {name}  [{atype}]{size_str}")
        lines.append("\n  Use /get N to download (e.g. /get 1)")
        return "\n".join(lines)

    def _list_sessions(self) -> str:
        """List recent conversation sessions from disk."""
        from pathlib import Path
        import json as _json

        conv_dir = Path.home() / ".aither" / "conversations"
        if not conv_dir.is_dir():
            return "No saved sessions found."

        sessions = []
        for f in conv_dir.glob("*.json"):
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                sid = data.get("session_id", f.stem)
                agent = data.get("agent_name", "?")
                msgs = data.get("messages", [])
                updated = data.get("updated_at", 0)
                sessions.append((updated, sid, agent, len(msgs)))
            except Exception:
                continue

        if not sessions:
            return "No saved sessions found."

        sessions.sort(reverse=True)  # newest first
        current = self.config.session_id or ""
        lines = [f"\n  Recent sessions ({len(sessions)} total):\n"]
        for updated, sid, agent, count in sessions[:15]:
            import datetime
            ts = datetime.datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M") if updated else "?"
            marker = " <-- current" if sid == current else ""
            lines.append(f"  {sid}  {agent:12s}  {count:3d} msgs  {ts}{marker}")
        if len(sessions) > 15:
            lines.append(f"  ... and {len(sessions) - 15} more")
        lines.append(f"\n  Resume with: /resume <session_id>")
        return "\n".join(lines)

    async def _download_artifact(self, arg: str) -> None:
        """Download an artifact by index, optionally to a custom path."""
        if not self._artifacts:
            print("  No artifacts to download.", file=sys.stderr)
            return

        parts = arg.split(None, 1)
        if not parts or not parts[0].isdigit():
            print("  Usage: /get N [dest_path]", file=sys.stderr)
            print(f"  Available: 1-{len(self._artifacts)}", file=sys.stderr)
            return

        idx = int(parts[0])
        if idx < 1 or idx > len(self._artifacts):
            print(f"  Invalid artifact #. Available: 1-{len(self._artifacts)}", file=sys.stderr)
            return

        art = self._artifacts[idx - 1]
        remote_path = art.get("file_path") or art.get("path") or art.get("url", "")

        if not remote_path:
            print("  Artifact has no downloadable path.", file=sys.stderr)
            return

        # Strip leading /api/files?path= or /api/files/ if present
        if "?path=" in remote_path:
            remote_path = remote_path.split("?path=", 1)[1]
        elif remote_path.startswith("/files/"):
            remote_path = remote_path[len("/files/"):]
        elif remote_path.startswith("/api/files/"):
            remote_path = remote_path[len("/api/files/"):]

        # Determine local filename
        filename = art.get("name") or art.get("filename") or remote_path.rsplit("/", 1)[-1]

        # Custom destination path
        if len(parts) > 1:
            dest = parts[1]
        else:
            dest = filename

        print(f"  Downloading artifact #{idx}: {filename} ...", file=sys.stderr, flush=True)
        try:
            saved = await self.genesis_client.download_file(remote_path, dest)
            print(f"  \u2713 Saved to: {saved}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  \u2717 Download failed: {e}", file=sys.stderr, flush=True)

    # ── Main processing loop ─────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Process inputs from the queue.

        When idle: start a new generation.
        When generating: steer the active session.
        """
        while not self._shutdown:
            try:
                text = await asyncio.wait_for(
                    self._input_queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            if text is None:  # shutdown sentinel
                break

            # Add to history
            self.history.append(text)
            self._save_history()

            # Built-in commands
            if text.startswith("/"):
                result = await self._handle_command(text)
                if result is not None:
                    print(result)
                if self._shutdown:
                    break
                continue

            # If generating, steer the active session
            if self._generating and self._active_session:
                print(f"  [steering → {text[:60]}]")
                await self.genesis_client.steer(
                    self._active_session, text, action="append",
                )
                continue

            # Start a new generation
            await self._run_generation(text)

    async def _on_event(self, event_type: str, data: dict) -> None:
        """Handle SSE events for pipeline visibility."""
        if event_type == "session_start":
            agent = data.get("agent", "aither")
            model = data.get("model", "auto")
            print(f"  [{agent}] model={model}", file=sys.stderr, flush=True)
        elif event_type == "pipeline":
            effort = data.get("effort", {})
            strategy = data.get("strategy", "")
            stage = data.get("stage", "")
            if effort and isinstance(effort, dict):
                print(
                    f"  [{effort.get('label', '')}] effort={effort.get('level', '?')}",
                    file=sys.stderr, flush=True,
                )
            elif stage == "agentic_promotion":
                cat = data.get("category", "")
                print(f"  [AGENTIC] promoted: {cat}", file=sys.stderr, flush=True)
            elif stage:
                msg = data.get("message", stage)
                print(f"  [{stage}] {msg}", file=sys.stderr, flush=True)
        elif event_type == "thinking":
            content = data.get("content") or ""
            if content and self.config.stream and self.config.show_thinking:
                if not self._thinking_active:
                    self._thinking_active = True
                    # Dim header — ANSI dim (2) for think tokens
                    print("\033[2m", end="", flush=True)
                print(content, end="", flush=True)
            elif content:
                # Non-streaming mode: show truncated summary on stderr
                print(f"  [think] {content[:120]}", file=sys.stderr, flush=True)
        elif event_type == "thinking_end":
            if self._thinking_active:
                # Close dim, print separator before answer
                print("\033[0m", flush=True)
                print("  ---", file=sys.stderr, flush=True)
                self._thinking_active = False
        elif event_type == "tool_call":
            tool = data.get("tool", data.get("name", "?"))
            print(f"  [tool] {tool}", file=sys.stderr, flush=True)
        elif event_type == "tool_result":
            tool = data.get("tool", data.get("name", "?"))
            ok = "ok" if data.get("success", True) else "FAIL"
            print(f"  [tool] {tool} → {ok}", file=sys.stderr, flush=True)
        elif event_type == "progress":
            msg = data.get("message", "")
            if msg:
                print(f"\033[2K  [{msg}]", file=sys.stderr, end="\r", flush=True)
        elif event_type == "token":
            text = data.get("t", "")
            if text and self.config.stream:
                if self._thinking_active:
                    print("\033[0m", flush=True)
                    print("  ---", file=sys.stderr, flush=True)
                    self._thinking_active = False
                if not self._tokens_displayed:
                    # Clear any residual progress line before first token
                    print("\033[2K", end="\r", file=sys.stderr, flush=True)
                    self._tokens_displayed = True
                print(text, end="", flush=True)
        elif event_type == "steering":
            action = data.get("action", "")
            msg = data.get("message", "")
            print(f"  [steer:{action}] {msg}", file=sys.stderr, flush=True)
        elif event_type in ("answer", "complete"):
            # Capture artifacts from answer/complete events (deduplicate)
            arts = data.get("artifacts") or []
            existing_ids = {a.get("id") for a in self._artifacts if a.get("id")}
            for art in arts:
                if isinstance(art, dict):
                    art_id = art.get("id")
                    if art_id and art_id in existing_ids:
                        continue
                    self._artifacts.append(art)
                    if art_id:
                        existing_ids.add(art_id)
            if event_type == "complete":
                dur = data.get("duration_ms", 0)
                model = data.get("model", "")
                print(
                    f"  [done] {dur}ms {model}",
                    file=sys.stderr, flush=True,
                )
                # Show artifact summary after generation
                if self._artifacts:
                    new_arts = self._artifacts[-len(arts):]  if arts else []
                    for i, a in enumerate(new_arts):
                        idx = len(self._artifacts) - len(new_arts) + i + 1
                        name = a.get("name") or a.get("filename") or a.get("path", "unknown")
                        atype = a.get("artifact_type") or a.get("type", "file")
                        size = a.get("size_bytes", 0)
                        size_str = f" ({size:,} bytes)" if size else ""
                        print(
                            f"  \U0001F4E6 Artifact #{idx}: {name}{size_str} [{atype}]"
                            f" — /get {idx} to download",
                            file=sys.stderr, flush=True,
                        )
        # heartbeat: silently ignored (liveness only)

    async def _run_generation(self, user_input: str) -> None:
        """Stream a chat request. Non-blocking — _input_loop keeps running."""
        # STABLE session for the whole shell run. The old code generated a
        # fresh uuid4 PER MESSAGE when config.session_id was unset — every
        # turn was a brand-new server-side session, so conversation history
        # never followed ([session_context] history_chars=0 on follow-ups,
        # live-observed 2026-07-02). Generate once, stick to it; /resume and
        # /new still override by setting/clearing config.session_id.
        if not self.config.session_id:
            self.config.session_id = str(uuid4())
            self.config.last_session_id = self.config.session_id
        session_id = self.config.session_id
        self._generating = True
        self._active_session = session_id
        self._thinking_active = False
        self._tokens_displayed = False

        # ── /deep prefix: per-message cloud escalation ──
        _extra_kwargs: dict = {}
        if user_input.startswith("/deep"):
            parts = user_input.split(" ", 1)
            prefix = parts[0]  # "/deep" or "/deep:model-name"
            user_input = parts[1] if len(parts) > 1 else user_input
            cloud_model = (
                prefix.split(":", 1)[1] if ":" in prefix else "deepseek-v4-flash"
            )
            _extra_kwargs["prefer_cloud_model"] = cloud_model
            print(
                f"  [cloud] routing to {cloud_model}",
                file=__import__("sys").stderr, flush=True,
            )

        try:
            async for chunk in self.genesis_client.chat_stream(
                message=user_input,
                persona=self.config.persona,
                effort=self.config.effort,
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                safety_level=self.config.safety_level,
                session_id=session_id,
                on_event=self._on_event,
                **_extra_kwargs,
            ):
                if self._shutdown:
                    break
                if self.config.stream and not self._tokens_displayed:
                    # If thinking was streaming and answer tokens start,
                    # close the dim block first
                    if self._thinking_active:
                        print("\033[0m", flush=True)
                        print("  ---", file=sys.stderr, flush=True)
                        self._thinking_active = False
                    print(chunk, end="", flush=True)

            print()  # newline after streaming response

        except GenesisError as e:
            print(f"\n[ERROR] {e.message}", file=sys.stderr)
        except Exception as e:
            print(f"\n[ERROR] {e}", file=sys.stderr)
        finally:
            # Clean up dim mode if thinking was still active
            if self._thinking_active:
                print("\033[0m", end="", flush=True)
                self._thinking_active = False
            # Auto-persist session_id so the next shell launch resumes this
            # conversation automatically (the messages are already on disk in
            # SQLite + JSON; only the session_id pointer was missing).
            if session_id:
                self.config.session_id = session_id
                self.config.last_session_id = session_id
                try:
                    from adk.shell.config import save_config
                    save_config(self.config)
                except Exception:
                    pass
            self._generating = False
            self._active_session = None

    # ── REPL entry point ─────────────────────────────────────────────

    async def run_repl(self) -> None:
        # Check Genesis health
        print("Checking Genesis connection...", file=sys.stderr)
        healthy = await self.genesis_client.health_check()
        if not healthy:
            print(
                "[ERROR] Genesis is not responding. Check your connection.",
                file=sys.stderr,
            )
            raise GenesisError("Genesis health check failed")

        print(f"Welcome to AitherShell {self.config.url}")
        print("Type /help for commands, Ctrl+C to cancel active generation\n")

        # Auto-restore last session so multi-turn context carries across restarts
        if not self.config.session_id and getattr(self.config, "last_session_id", None):
            self.config.session_id = self.config.last_session_id
            print(
                f"  Resumed session {self.config.session_id}"
                f"  (type /new for a fresh session)\n",
                file=sys.stderr,
            )

        # Run input reader and processor concurrently
        input_task = asyncio.create_task(self._input_loop())
        process_task = asyncio.create_task(self._process_loop())

        try:
            # Wait for either to finish (usually _process_loop on shutdown)
            done, pending = await asyncio.wait(
                [input_task, process_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel the other
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown = True
            print("\nGoodbye!")
            await self.genesis_client.close()


async def run_repl(config: AitherConfig) -> None:
    """Run the AitherShell REPL."""
    repl = AitherREPL(config)
    await repl.run_repl()

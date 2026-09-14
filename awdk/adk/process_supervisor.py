"""Process supervisor for native consumer stack — manage Room, Ollama as system processes.

The $1000 consumer SKU runs the sovereign stack as NATIVE processes:
  - AitherRoom (:8350)  — company/partner agent room
  - Ollama (:11434)     — local model serving

This module provides:
  - ProcessSpec: process definition (name, argv, port, health_url, cwd, env)
  - ProcessSupervisor: spawn, monitor, restart with backoff, graceful shutdown
  - Per-process logging with size-based rotation (5MB, keep 3)
  - Port-conflict detection with actionable errors
  - SIGINT handler → graceful terminate-then-kill
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adk.process_supervisor")

_LOG_DIR = Path.home() / ".aither" / "logs"
_MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB
_MAX_LOG_FILES = 3


@dataclass
class ProcessSpec:
    """Specification for a managed process."""

    name: str
    argv: list[str]
    port: int
    health_url: str  # Relative or absolute URL, e.g. "/health" or "http://localhost:8350/health"
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None  # Additional env vars to merge
    description: str = ""


class ProcessSupervisor:
    """Spawn and supervise a set of processes with health checking and restarts."""

    def __init__(self):
        """Initialize the supervisor."""
        self.processes: dict[str, subprocess.Popen] = {}
        self.restart_counts: dict[str, int] = {}
        self.last_healthy: dict[str, float] = {}
        self.backoff_delays: dict[str, float] = {}  # Current backoff in seconds

    def check_port_available(self, port: int, name: str = "") -> tuple[bool, str]:
        """Check if a port is free. Returns (available, error_message)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                result = s.connect_ex(("127.0.0.1", port))
                if result == 0:
                    detail = f" ({name})" if name else ""
                    return False, f"Port {port} is already in use{detail}"
                return True, ""
        except Exception as e:
            return False, f"Could not check port {port}: {e}"

    def _setup_logging(self, name: str) -> logging.FileHandler:
        """Set up per-process log file with rotation."""
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOG_DIR / f"{name}.log"

        # Rotate old logs: keep _MAX_LOG_FILES
        if log_path.exists() and log_path.stat().st_size > _MAX_LOG_SIZE:
            for i in range(_MAX_LOG_FILES - 1, 0, -1):
                old = _LOG_DIR / f"{name}.log.{i}"
                new = _LOG_DIR / f"{name}.log.{i + 1}"
                if old.exists():
                    old.replace(new) if new.exists() is False else None
            log_path.rename(_LOG_DIR / f"{name}.log.1")

        handler = logging.FileHandler(log_path, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        return handler

    def start(self, spec: ProcessSpec) -> tuple[bool, str]:
        """Start a process. Returns (success, message)."""
        # Pre-flight: port check
        available, err = self.check_port_available(spec.port, spec.name)
        if not available:
            return False, err

        # Build environment
        env = os.environ.copy()
        if spec.env:
            env.update(spec.env)

        # Log setup
        self._setup_logging(spec.name)

        try:
            proc = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd or os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.processes[spec.name] = proc
            self.restart_counts[spec.name] = 0
            self.last_healthy[spec.name] = time.time()
            self.backoff_delays[spec.name] = 1.0
            logger.info(
                "%s started (PID %d, port %d)",
                spec.name,
                proc.pid,
                spec.port,
            )
            return True, f"Started {spec.name} (PID {proc.pid})"
        except Exception as e:
            return False, f"Failed to start {spec.name}: {e}"

    def is_healthy(self, spec: ProcessSpec, timeout: int = 5) -> bool:
        """Check if a process is healthy via HTTP health check."""
        if spec.name not in self.processes:
            return False

        proc = self.processes[spec.name]
        if proc.poll() is not None:  # Process has exited
            return False

        # Build health URL
        url = spec.health_url
        if not url.startswith("http"):
            url = f"http://127.0.0.1:{spec.port}{url}"

        try:
            import urllib.request

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def restart(self, spec: ProcessSpec, reason: str = "health check failed") -> bool:
        """Restart a process with exponential backoff."""
        name = spec.name
        if name not in self.processes:
            return False

        # Terminate old process
        proc = self.processes[name]
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except Exception as e:
            logger.warning("%s termination error: %s", name, e)

        # Exponential backoff: 1s → 2s → 4s → ... → 60s
        delay = self.backoff_delays.get(name, 1.0)
        if delay < 60:
            self.backoff_delays[name] = delay * 2
        else:
            self.backoff_delays[name] = 60

        # Reset backoff after 10 minutes of healthy operation
        healthy_time = time.time() - self.last_healthy.get(name, time.time())
        if healthy_time > 600:
            self.backoff_delays[name] = 1.0

        count = self.restart_counts.get(name, 0) + 1
        self.restart_counts[name] = count

        logger.warning(
            "%s restart #%d (%s), waiting %.1fs",
            name,
            count,
            reason,
            delay,
        )
        time.sleep(delay)

        _, msg = self.start(spec)
        logger.info("Restart result: %s", msg)
        return True

    def shutdown_gracefully(self, timeout: int = 10) -> None:
        """Gracefully shutdown all processes (terminate, then kill)."""
        if not self.processes:
            return

        names = list(self.processes.keys())
        logger.info("Shutting down %d processes: %s", len(names), ", ".join(names))

        # Terminate all
        for name, proc in self.processes.items():
            if proc.poll() is None:
                try:
                    proc.terminate()
                    logger.debug("%s terminated", name)
                except Exception as e:
                    logger.warning("%s terminate error: %s", name, e)

        # Wait for graceful shutdown
        start = time.time()
        while time.time() - start < timeout:
            still_running = [
                n
                for n, p in self.processes.items()
                if p.poll() is None
            ]
            if not still_running:
                logger.info("All processes shut down gracefully")
                return
            time.sleep(0.5)

        # Force kill any stragglers
        for name, proc in self.processes.items():
            if proc.poll() is None:
                try:
                    proc.kill()
                    logger.warning("%s force-killed", name)
                except Exception as e:
                    logger.warning("%s kill error: %s", name, e)

        # Final wait
        for proc in self.processes.values():
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

        logger.info("Process shutdown complete")

    async def _enroll_fleet_async(self) -> None:
        """Enroll node into fleet (async wrapper for supervise loop).

        Attempts enrollment after services are up. Failures are logged but
        do not interrupt supervision.
        """
        try:
            from adk.fleet_enroll import enroll_on_boot

            result = await enroll_on_boot()
            if result.get("enrolled"):
                logger.info(
                    "Node enrolled: %s (agents=%d)",
                    result.get("node_id"),
                    result.get("agents_upserted", 0),
                )
            else:
                reason = result.get("reason", result.get("error", "unknown"))
                logger.info("Node enrollment skipped: %s", reason)
        except ImportError:
            logger.debug("fleet_enroll module not available (may be offline)")
        except Exception as e:
            logger.warning("Fleet enrollment error: %s", e)

    def supervise(
        self,
        specs: list[ProcessSpec],
        interval: int = 10,
    ) -> None:
        """Supervise all processes in a foreground loop.

        Monitors health, restarts on failure, handles SIGINT gracefully.
        This is a blocking call.
        """
        import signal

        def _sigint_handler(signum, frame):
            logger.info("Received SIGINT, shutting down...")
            self.shutdown_gracefully()
            sys.exit(0)

        signal.signal(signal.SIGINT, _sigint_handler)

        # Start all
        for spec in specs:
            success, msg = self.start(spec)
            if success:
                logger.info(msg)
            else:
                logger.error(msg)
                sys.exit(1)

        logger.info("All processes started, entering supervision loop (interval=%ds)", interval)

        # Enroll into fleet (best-effort). Run in a daemon thread with a
        # PERSISTENT event loop: enrollment schedules a background heartbeat
        # task, and a bare asyncio.run() would close the loop the moment
        # enrollment returns — killing the heartbeat so the node goes stale
        # after ~60s. run_forever() keeps the loop (and heartbeat) alive.
        def _fleet_enroll_bg():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._enroll_fleet_async())
                loop.run_forever()
            except Exception as e:
                logger.debug("Fleet enrollment thread ended: %s", e)

        threading.Thread(target=_fleet_enroll_bg, daemon=True, name="fleet-enroll").start()
        try:
            while True:
                for spec in specs:
                    if spec.name in self.processes:
                        if not self.is_healthy(spec):
                            logger.warning("%s health check failed", spec.name)
                            self.restart(spec)
                        else:
                            self.last_healthy[spec.name] = time.time()
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Supervision interrupted, shutting down...")
            self.shutdown_gracefully()
            sys.exit(0)


def resolve_room_launch_target() -> Optional[str]:
    """Resolve the Room service launch target from the installation.

    Returns the path to the Room binary (consumer SKU default) or a Python module
    path (e.g., "aither_room.app:app") for development.

    Resolution order:
      1. AITHER_ROOM_LAUNCH_TARGET env var (e.g., "apps.AitherRoom.room_service:app"
         for dev, or the binary path for consumer)
      2. Check if "aither-room" is installed as a package
      3. Look for a "room" or "aither-room" repo relative to adk
      4. Fall back to room_launcher.get_room_binary() (consumer SKU binary)
      5. Return None (Room app not available)
    """
    env_target = os.environ.get("AITHER_ROOM_LAUNCH_TARGET", "").strip()
    if env_target:
        return env_target

    # Try to import aither_room to check if installed
    try:
        import aither_room  # noqa: F401

        return "aither_room.app:app"
    except ImportError:
        pass

    # Check relative to adk
    candidates = [
        Path(__file__).resolve().parents[2] / "aither-room",
        Path(__file__).resolve().parents[1] / "room",
        Path.cwd() / "aither-room",
    ]
    for candidate in candidates:
        if (candidate / "setup.py").exists() or (candidate / "pyproject.toml").exists():
            return f"{candidate}:app"

    # Fall back to consumer SKU: room_launcher binary
    from adk.room_launcher import get_room_binary

    binary = get_room_binary()
    if binary:
        return str(binary)

    return None

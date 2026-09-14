"""
``aither watch`` — fleet watchtower with wedge-signature detection.
====================================================================

A slow heartbeat loop (default 15s) that prints one status line per tick and
raises its voice only on *changes*. It knows this fleet's recurring failure
signatures and names them instead of dumping symptoms:

- **docker-wsl wedge** — docker API dead/500s → offers ``--auto-recover`` to
  run the same recovery as ``aither docker recover``.
- **llm-queue wedge** — MicroScheduler shows queued work but zero in-flight
  for 3 consecutive ticks (the recurring "AitherChat offline" cause).
- **service flap** — a service's /health uptime goes *down* between ticks
  (crash-loop / restart, e.g. the CommCore boot-hang).
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import click

from adk.shell.command_center.fleet_client import FleetClient


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _say(msg: str, fg: Optional[str] = None, bold: bool = False):
    try:
        click.echo(click.style(f"[{_now()}] {msg}", fg=fg, bold=bold))
    except UnicodeEncodeError:
        click.echo(f"[{_now()}] {msg}".encode("ascii", "replace").decode())


async def _watch_loop(interval: float, auto_recover: bool) -> None:
    fc = FleetClient()
    prev_up: dict[str, bool] = {}
    prev_uptime: dict[str, float] = {}
    stalled_ticks = 0
    docker_bad_ticks = 0
    try:
        while True:
            services = await fc.service_health()
            queue = await fc.llm_queue()

            # Per-service transitions + uptime regressions (crash-loops).
            flaps = []
            for name, st in services.items():
                was_up = prev_up.get(name)
                if was_up is not None and was_up != st.ok:
                    _say(f"{name} {'RECOVERED' if st.ok else 'WENT DOWN: ' + st.error}",
                         fg="green" if st.ok else "red", bold=True)
                prev_up[name] = st.ok
                if st.ok and isinstance(st.data, dict):
                    up = st.data.get("uptime_sec") or st.data.get("uptime_seconds")
                    if isinstance(up, (int, float)):
                        if name in prev_uptime and up < prev_uptime[name]:
                            flaps.append(name)
                        prev_uptime[name] = up
            for name in flaps:
                _say(f"{name} RESTARTED (uptime went backwards) — crash-loop?",
                     fg="yellow", bold=True)

            # LLM-queue wedge signature.
            if queue.ok and isinstance(queue.data, dict):
                queued = int(queue.data.get("queued",
                             queue.data.get("pending_tasks", 0)) or 0)
                processing = int(queue.data.get("processing",
                                 queue.data.get("running_tasks", 0)) or 0)
                stalled_ticks = stalled_ticks + 1 if (queued > 0 and processing == 0) else 0
                if stalled_ticks == 3:
                    _say(f"LLM-QUEUE WEDGE: {queued} queued, 0 in-flight for "
                         f"{3 * interval:.0f}s — MicroScheduler dispatch is stuck "
                         "(known signature; check :8150/llm/queue/detail)",
                         fg="red", bold=True)
            else:
                stalled_ticks = 0

            # Docker wedge signature.
            docker_ok = True
            try:
                from adk.shell.cli import _docker_healthy
                docker_ok = _docker_healthy()
            except Exception:
                pass
            docker_bad_ticks = 0 if docker_ok else docker_bad_ticks + 1
            if docker_bad_ticks == 2:
                _say("DOCKER WEDGE: API unresponsive for 2 ticks", fg="red", bold=True)
                if auto_recover:
                    _say("auto-recovering docker...", fg="yellow")
                    from adk.shell.cli import _docker_recover
                    _docker_recover(verbose=True)

            up = sum(1 for s in services.values() if s.ok)
            q_txt = "?"
            if queue.ok and isinstance(queue.data, dict):
                q_txt = str(queue.data.get("queued",
                            queue.data.get("pending_tasks", "?")))
            _say(f"fleet {up}/{len(services)} up  queue={q_txt}"
                 + ("" if docker_ok else "  docker=DOWN"),
                 fg=None if up == len(services) and docker_ok else "yellow")
            await asyncio.sleep(interval)
    finally:
        await fc.close()


def run_watch(interval: float = 15.0, auto_recover: bool = False) -> None:
    _say(f"watchtower up (tick {interval:g}s"
         + (", docker auto-recover ON" if auto_recover else "") + "). Ctrl+C to stop.")
    try:
        asyncio.run(_watch_loop(interval, auto_recover))
    except KeyboardInterrupt:
        _say("watchtower stopped.")

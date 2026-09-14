"""Daemon entrypoint for `adk claude-model watch --daemon`.

Runs the supervision loop in its own process so the parent can exit. Lives in a
module (not an inline `-c`) so the child has a real import path and a traceback
that names a file — a detached child that dies inside a `-c` string is close to
undebuggable, and this process only matters when something else has already
gone wrong.
"""

from __future__ import annotations

import time

from adk.claude_model import _WATCH_INTERVAL, _watch_log, _watch_once


def main() -> int:
    _watch_log(f"watch daemon up (every {_WATCH_INTERVAL}s)")
    while True:
        try:
            _watch_once()
        except Exception as exc:  # never let one bad pass kill the supervisor
            _watch_log(f"pass raised {type(exc).__name__}: {exc}")
        time.sleep(_WATCH_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())

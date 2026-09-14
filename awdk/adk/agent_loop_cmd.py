"""Entry point for running an agent loop via `python -m adk.agent_loop_cmd`.

This is what `adk agent run` spawns as a detached process.
"""

from __future__ import annotations

import argparse
import sys


def _main():
    """Parse args and run the agent loop."""
    from adk.agent_loop import main

    parser = argparse.ArgumentParser(
        prog="adk.agent_loop_cmd",
        description="Run a host-tier agent loop",
    )
    parser.add_argument("name", help="Agent name")
    parser.add_argument("--daemon-url", default="http://127.0.0.1:8362",
                        help="Harness daemon URL")
    parser.add_argument("--token", help="Bearer token for daemon")
    parser.add_argument("--room", default="main", help="Room name")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Loop interval in seconds")

    args = parser.parse_args()

    sys.exit(main(
        agent_name=args.name,
        daemon_url=args.daemon_url,
        daemon_token=args.token or "",
        room=args.room,
        interval=args.interval,
    ))


if __name__ == "__main__":
    _main()


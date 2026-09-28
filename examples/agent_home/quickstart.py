"""Agent Home quickstart -- no model, no network, no account.

Starts the reference ``aither-game/1`` server (a treasure hunt) on localhost,
sends your agent in for a few sessions, and prints how it improves.

    python examples/agent_home/quickstart.py

Learning that persists between sessions needs the ``agent-home`` license pack;
without it this runs every session from scratch (and says so), which is the
point of comparison.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from adk.games import open_game
from adk.games.fake_server import TreasureLineGame, serve_in_thread
from adk.games.learning import GameLearner
from adk.home.entitlement import has_pack


def main() -> None:
    httpd, base = serve_in_thread(TreasureLineGame(length=6))
    persist = has_pack()
    state_dir = Path(tempfile.mkdtemp(prefix="agent-home-demo-"))
    print(f"game server: {base}/game   learning persists: {persist}")
    try:
        for session in range(1, 7):
            with open_game(f"{base}/game", name="demo") as game:
                learner = GameLearner(game, persist=persist, state_dir=state_dir,
                                      seed=session, epsilon=0.05)
                r = learner.play_session(budget=40)
            print(f"session {session}: {r.steps:2d} steps  reward {r.total_reward:+.2f}"
                  f"  surprise {r.mean_surprise}  found treasure={r.done}")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()

"""Send your Agent Home agent into a Saga story room.

    # a local Saga (pip install aither-saga, or the .SAGA product folder):
    python examples/agent_home/play_saga.py saga+http://127.0.0.1:8793?world=elysium

    # no Saga handy? a scripted stand-in:
    python -m adk.games.fake_server --game saga --port 8799 &
    python examples/agent_home/play_saga.py saga+http://127.0.0.1:8799?world=elysium

If `adk home model` is set, the agent's own model picks actions (and writes the
story's prose when the Saga server has no model of its own); otherwise it plays
on learned values and curiosity.
"""

from __future__ import annotations

import sys

from adk.games import open_game
from adk.games.learning import GameLearner, game_memory, llm_policy
from adk.home import config as hc
from adk.home import models
from adk.home.entitlement import has_pack


def main(url: str, steps: int = 8) -> None:
    policy = None
    name = "agent-home"
    if hc.is_initialized():
        cfg = hc.load_config()
        name = cfg.name
        try:
            policy = llm_policy(models.build_llm(cfg.model), hc.compose_system_prompt())
        except hc.HomeError as exc:
            print(f"(model not usable: {exc}; playing on learned values)")
    persist = has_pack()
    with open_game(url, name=name) as game:
        learner = GameLearner(game, policy=policy, persist=persist,
                              memory=game_memory(name) if persist else None)
        report = learner.play_session(budget=steps)
    for t in report.trace:
        print(f"{t.step:2d}. [{t.why:7s}] {t.action:<32} reward {t.reward:+.2f}")
    print(f"total {report.total_reward:+.2f}; remembered for next time: {report.persisted}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8)

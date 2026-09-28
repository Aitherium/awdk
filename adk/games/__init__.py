"""adk.games -- let an agent join games: connect, observe, act, chat, learn.

    from adk.games import open_game
    with open_game("saga+http://127.0.0.1:8793?world=elysium") as game:
        obs = game.join()
        step = game.act(obs.actions[0])

See ``docs/agent-home.md``. Submodules: ``base`` (contract), ``generic``
(aither-game/1), ``saga`` (Saga rooms), ``env_adapter`` (world-model bridge),
``learning`` (cross-session learning), ``fake_server`` (reference servers).
"""

from .base import (
    GameClient,
    GameError,
    Observation,
    StepResult,
    known_game_kinds,
    open_game,
    register_game_client,
    split_token,
)

__all__ = [
    "GameClient",
    "GameError",
    "Observation",
    "StepResult",
    "known_game_kinds",
    "open_game",
    "register_game_client",
    "split_token",
]

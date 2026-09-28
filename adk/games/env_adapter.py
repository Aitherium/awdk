"""GameEnvAdapter -- any joinable game as a world-model EnvironmentAdapter.

This is the bridge into the existing learn-safely loop: ``env_enroll`` and
``explore`` (``adk.packs.world_model``) drive ``observe / actions / step`` and a
``domain`` string, exactly as they drive the ARC adapter. So::

    from adk.packs.world_model.env_enroll import env_enroll
    env_enroll("adk.games.env_adapter:GameEnvAdapter",
               {"url": "saga+http://127.0.0.1:8793?world=elysium"})

explores a Saga room under a hard step budget and writes a sandbox proof when
the world model's surprise on that room falls. env_enroll builds a FRESH adapter
per episode, and each fresh adapter joins the room fresh.

Observations handed to the world model are the game's state SIGNATURE (stable,
prose-free), never the narrative text -- a paragraph that never repeats is
unlearnable by construction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import GameClient, GameError, Observation, open_game


class GameEnvAdapter:
    """EnvironmentAdapter over a :class:`GameClient`."""

    def __init__(self, url: str = "", token: Optional[str] = None,
                 name: str = "agent-home", kind: Optional[str] = None,
                 client: Optional[GameClient] = None, **client_kwargs: Any) -> None:
        if client is None:
            if not url:
                raise GameError("GameEnvAdapter needs a url or a client")
            client = open_game(url, token=token, name=name, kind=kind,
                               **client_kwargs)
        self.client = client
        self._obs: Optional[Observation] = None
        self._obs = self.client.join() if not self.client.joined \
            else self.client.observe()
        self.domain = self.client.domain

    @property
    def last_observation(self) -> Optional[Observation]:
        return self._obs

    def observe(self, env_state: Any = None) -> str:
        if env_state is not None:
            return str(env_state)
        if self._obs is None:
            self._obs = self.client.observe()
        return self._obs.signature

    def actions(self) -> Sequence[str]:
        if self._obs is None:
            self._obs = self.client.observe()
        return list(self._obs.actions)

    def _resolve(self, action: Any) -> str:
        acts: List[str] = list(self.actions())
        if isinstance(action, int) and not isinstance(action, bool):
            if not acts:
                raise GameError("no legal actions right now")
            return acts[action % len(acts)]
        return str(action)

    def step(self, action: Any) -> Tuple[str, float, bool, Dict[str, Any]]:
        res = self.client.act(self._resolve(action))
        self._obs = res.observation
        return res.observation.signature, float(res.reward), bool(res.done), \
            dict(res.info)

    def close(self) -> None:
        self.client.close()

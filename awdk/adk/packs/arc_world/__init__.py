"""arc_world — an EnvironmentAdapter over the PUBLIC ARC-AGI-3 game API, for
env_enroll.

B1 of the ARC BYO-cognition program: with this adapter, ``env_enroll`` can enroll
a real ARC game out of the box — the learn-safely explore loop drives it, the
local world model learns the game's dynamics, and every transition is contributed
to the public Contribution Gateway (quarantined, trust-scored, leaderboard).

Importable dotted path (the ``arc-brainpack`` pack dir is hyphenated, so the
adapter cannot live there as an importable module):
    from adk.packs.arc_world import ArcGatewayAdapter
    env_enroll("adk.packs.arc_world:ArcGatewayAdapter",
               adapter_kwargs={"game_id": "ls20"})

The 5-line BYO-agent flow (arc_* tools are agent tools from pack "arc-brainpack",
file-loaded by the loader — the pack dir is hyphenated, never import it dotted):
    1  activate pack "arc-brainpack" on the agent     # arc_register/arc_status/...
    2  arc_register(handle="you")                     # wallet -> contributor token
    3  export ARC_API_KEY=...                         # reach the real game API
    4  env_enroll("adk.packs.arc_world:ArcGatewayAdapter",
                   adapter_kwargs={"game_id": "ls20"})
    5  arc_status()                                   # accepted count on the board
"""

from .adapter import ArcGatewayAdapter

__all__ = ["ArcGatewayAdapter"]

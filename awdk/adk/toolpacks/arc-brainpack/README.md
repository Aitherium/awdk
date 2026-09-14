# ARC Brain Pack — connect your agent to the ARC world model

The AitherWorldModel is trained by the world — humans play on
[arc.aitherium.com](https://arc.aitherium.com), the built-in solver plays 24/7,
and now **your agent can join**: play real ARC-AGI-3 games with your own policy,
learn them in your own local mini world model, and contribute every transition to
the shared model's quarantine (trust-scored, shown on the leaderboard).

This pack is fully self-contained (`pip install "awdk[arc]"` gives you
everything — no clone of any other repo).

## Connect your agent in 5 lines

The pack's tools (`arc_register`, `arc_enroll`, `arc_status`, …) are activated on
the agent **by name** — activate pack `"arc-brainpack"` (the pack dir is
hyphenated, so the loader file-loads it; it is never imported as a dotted
module). The adapter + enroll driver are importable dotted paths:

```python
from adk.packs.arc_world import ArcGatewayAdapter          # the EnvironmentAdapter
from adk.packs.world_model.env_enroll import env_enroll    # the enroll driver

# 1. mint a contributor token (agent tool "arc_register", wallet -> token, persisted):
#    arc_register(handle="you")
# 2. export ARC_API_KEY=...              # the real ARC-AGI-3 game API key
# 3. play + learn + contribute in one call:
env_enroll("adk.packs.arc_world:ArcGatewayAdapter",
           adapter_kwargs={"game_id": "ls20"},
           episodes=6, budget=30)
# 4. your accepted count on the board:   arc_status()
# 5. see yourself ranked:                arc_leaderboard()
```

That is the whole loop: register → enroll → the learn-safely explore loop drives
your game, the local mini world model (in-process when `AITHER_OFFLINE=1`, else
`:8197 domain=arc`) learns its dynamics, every `(grid, action, next_grid)`
transition is submitted to `POST /contribute/v1/observe` (quarantined until the
offline validator promotes it), and your standing updates on the leaderboard.

## The tools

| tool | what it does |
|---|---|
| `arc_register([handle])` | wallet → contributor Bearer token, persisted to `~/.aitheros/arc_contrib_token.json` (+ `WM_CONTRIB_TOKEN`). Idempotent. |
| `arc_contribute(games, n)` | play real games with the built-in uniform-random policy, submit every transition. |
| `arc_enroll(game, episodes, budget)` | **BYO policy entry point**: env_enroll over `ArcGatewayAdapter`. Play with your OWN LLM/agent, learn locally, contribute. |
| `arc_status()` | this token's server-side accepted count / quarantine path. |
| `arc_leaderboard([limit])` | who's taught the model the most. |
| `arc_solo()` | one-command bootstrap for your OWN world-model + gateway stack (loopback-only). |

Every tool is best-effort and never raises into your agent's loop. A dead
gateway, a missing token, or a missing `ARC_API_KEY` produces a readable message,
not a crash. Contribution is always quarantined first — nothing a contributor
sends reaches the resident model in real time.

## How the ArcGatewayAdapter fits

`ArcGatewayAdapter` (`adk.packs.arc_world`) implements the
`world_model.contracts.EnvironmentAdapter` contract
(`observe()` / `actions()` / `step()` / `.domain`), so `env_enroll` accepts it
out of the box. Each adapter instance = one game session (RESET on
construction); env_enroll builds a fresh instance per episode. `step()`:

1. picks/executes one ARC action on the real game API,
2. submits the `(pre, action, post)` transition to the gateway (quarantine),
3. returns `(next_obs, reward, done, info)` for the local world model.

Requires `ARC_API_KEY` to reach the real game. Contribution additionally wants a
token — without one the game is still played and learned **locally**; only the
gateway half is skipped, so a first-timer can try it with zero setup.

# Agent Home — host your own agent, and send it into games

Agent Home is the awdk kit for running **your own** agent on **your own**
machine: you pick its model, write its persona, choose the harness that runs
it, and let it join games, where it explores, remembers what worked, and plays
better the next time.

```bash
pip install awdk
adk home init --name pip                  # creates ~/.aither/agent-home
adk home model --local bonsai             # or: --byo deepseek | openai | anthropic
adk home harness aither                   # or: claude | openclaw | hermes
adk home join "saga+http://127.0.0.1:8793?world=elysium" --steps 10
```

Every command also runs as `python -m adk.home …`.

## 1. The home folder

`~/.aither/agent-home` (override with `AITHER_AGENT_HOME`):

| path | what it is |
|---|---|
| `home.json` | name, model choice, harness choice |
| `persona/system_prompt.md` | the system prompt; edit freely |
| `persona/persona.md` | who the agent is: voice, values, goals |
| `persona/rules.md` | hard rules it always keeps |
| `harness/` | generated configs for Claude Code / OpenClaw / Hermes |
| `games/` | what it learned, one file per game room |
| `memory/` | its local memory database (adk `Memory`) |

`adk home persona` prints the three files; `adk home persona set persona.md -`
replaces one from stdin; `adk home persona path` prints the folder so you can
open it in your editor. The agent's system prompt is the three files joined in
that order.

## 2. Model: local or bring your own key

| choice | what it needs |
|---|---|
| `--local bonsai` | a Bonsai llama-server on `:8080` (`curl -fsSL https://aitherium.com/install-bonsai.sh \| sh`) |
| `--local llamacpp` | any `llama-server -m model.gguf --port 8080` |
| `--local ollama` | Ollama on `:11434` (`ollama pull gemma4:4b`) |
| `--byo deepseek` | `DEEPSEEK_API_KEY` in your environment |
| `--byo openai` | `OPENAI_API_KEY` |
| `--byo anthropic` | `ANTHROPIC_API_KEY` |

`--model`, `--base-url` and `--key-env` override the defaults. The config
stores the **name** of the variable that holds your key, never the key.
`adk home model --check` probes the endpoint (or checks the key is set).

## 3. Harness: who runs the loop

| harness | what `adk home harness <kind>` does |
|---|---|
| `aither` | adk's native agent loop wearing your persona. Nothing to install. `adk home chat "hi"` talks to it. |
| `claude` | writes `harness/CLAUDE.md` from the persona; start Claude Code in that folder. |
| `openclaw` | renders adk's OpenClaw connect template pointed at **your** model, plus a system-prompt file. |
| `hermes` | same for Hermes (`~/.hermes/cli-config.yaml` shape). |

Generated files reference your key as `${ENV_VAR}`; nothing edits another
program's settings in place — the output says where to merge.

## 4. Games

```bash
adk home join <game-url>                         # join and look
adk home join <game-url> --act "go to Market"    # one action
adk home join <game-url> --say "hello everyone"  # chat
adk home join <game-url> --steps 20 --sessions 3 --learn
adk home join <game-url> --steps 20 --policy model   # your model picks actions
```

A token can ride in the URL (`?token=…` or `#token=…`), in `--token`, or in
`AITHER_GAME_TOKEN`; it is stripped from the URL and never written to disk.

### Saga rooms

`saga+http://host:port?world=<id>`, `http://host/api/local/saga?world=<id>`,
or `https://saga.example.com/play/<id>`. The client speaks Saga's play API
(`/status`, `/worlds`, `/worlds/{id}/seed`, `/turn`, `/turns`, `/continuity`)
with the room in `X-Saga-World`. Saga keeps no score, so the agent's reward is
shaped from what Saga reports: a small reward per turn, more for each story
element it discovers, a penalty for each new continuity issue (it contradicted
the story). If the Saga server has no model configured, the agent writes the
turn's prose with its own model (Saga's `prose` field).

### Any other game: `aither-game/1`

Five JSON routes under one base URL:

```
POST {base}/join   {"name"}    -> {"session", "room", ...state}
GET  {base}/state              -> state
POST {base}/act    {"action"}  -> state (+ "reward", "done")
POST {base}/chat   {"text"}    -> {"ok": true}
POST {base}/leave              -> {"ok": true}

state = {"text", "state": {...}, "actions": [...], "reward", "done",
         "messages": [...], "signature"?}
```

Calls after join carry `X-Game-Session`. Keep free text in `text` and the
situation in `state`/`signature`: the agent learns on the signature, and a
paragraph that never repeats cannot be learned. `adk/games/fake_server.py` is
the reference implementation (`python -m adk.games.fake_server --port 8799`).

New clients plug in with `adk.games.register_game_client(kind, matcher, factory)`.

## 5. How the agent learns

Each session (`adk.games.learning.GameLearner`):

1. **Recall** the room's transition model and action values from `games/`.
2. **Play** a bounded number of steps. Your model picks when `--policy model`;
   otherwise learned values, with curiosity toward actions never tried in that
   situation — the same "highest surprise first" rule as the world-model
   pack's `safe_explore`.
3. **Record** each transition in the local model (surprise = how badly it
   predicted the next state), in the shared world-model pack (`wm_observe`,
   fail-soft), and a session summary with lessons in adk memory.
4. **Save**, so the next session starts smarter. `progress()` reports reward
   by session and whether it is improving.

`adk home enroll <game-url>` runs the world-model pack's `env_enroll` on the
room through `adk.games.env_adapter:GameEnvAdapter` — the same path the ARC
adapter uses — and writes a sandbox proof when surprise falls.

## 6. Free vs the Agent Home kit

| | free | `agent-home` pack |
|---|---|---|
| set up, persona, model, harness | yes | yes |
| join, observe, act, chat, play a session | yes | yes |
| learning that persists across sessions (`--learn`) | — | yes |
| world-model enrollment (`adk home enroll`) | — | yes |
| more than one agent (`--agents N`) | — | yes |

Buy the kit at <https://aitherium.com/shop/agent-home>, then:

```bash
adk home license ~/Downloads/agent-home-license.json   # or paste the text
adk home status
```

The license is verified (Ed25519, offline) before it is saved to
`~/.aither/license.json`; a license that does not verify is refused and
nothing is overwritten. adk reads one license file, so if the installed
license grants a pack the new one does not (say you bought Deep Research
earlier), the command refuses with exit `2`, names the packs that would stop
working, and saves nothing. Ask the shop to resend your licenses (it issues
one license listing every pack you own) and install that one. `--replace`
installs anyway; the old license is then kept as `license.json.<time>.bak`.

Exit codes: `0` ok · `1` the game or model failed · `2` setup or arguments ·
`3` needs the `agent-home` pack.

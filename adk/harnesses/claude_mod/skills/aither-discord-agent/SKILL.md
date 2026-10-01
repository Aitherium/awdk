---
name: aither-discord-agent
description: >-
  Deploy your awdk agent as a Discord bot (automated onboarding). Your awdk agent already has an identity, tools, memory, and inference on your own machine. This skill turns it into a Discord bot with one automated onboarding command — adk onboard --discord — which installs your agent pack, validates your bot token live against Discord's API, prints the invite link, verifies your identity + tools, and can launch the bot. Every DM or @mention then runs your agent's own loop: its tools, its memory, its personality.
---

# aither-discord-agent — deploy your awdk agent as a Discord bot (automated onboarding)

Your awdk agent already has an identity, tools, memory, and inference on your own machine.
This skill turns it into a **Discord bot** with **one automated onboarding command** —
`adk onboard --discord` — which installs your agent pack, **validates your bot token live against
Discord's API**, prints the invite link, verifies your identity + tools, and can launch the bot.
Every DM or @mention then runs your agent's own loop: its tools, its memory, its personality.

Program and customize behavior by editing the pack, not the bot.

## What you need

- A Discord account and a server you can add a bot to.
- A machine that already runs awdk (see the `aither-start` skill to get there).

## Step 1 — create the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** → **Reset Token** → copy it. This is your secret — treat it like a password.
3. Under **Bot** → **Privileged Gateway Intents**, enable **Message Content Intent** (the bot needs
   to read what people type).

That's all you create. You do **not** hand-build an invite link — onboarding generates it for you.

## Step 2 — run the automated onboarding

```bash
pip install 'awdk[channels]'      # the toolkit + discord.py
adk onboard --discord --identity <your-agent> --pack <your-pack>
```

It walks you through, one automated check at a time:

1. **Installs your agent pack** (`adk install pack:<your-pack>`).
2. **Reads the bot token** — `--token` / `DISCORD_BOT_TOKEN` / prompt.
3. **Validates the token live** against Discord's API (`GET /users/@me`). A typo'd or revoked token
   fails here with a clear message — no silent bot that never connects.
4. **Prints the invite link** (derived from your token) — open it, pick your server, Authorize.
5. **Verifies your identity + tools** resolve (the "identity did not resolve" gate).
6. **Launches the bot** when you add `--run`:
   `adk onboard --discord --identity <your-agent> --run`

**Check that can fail:** the onboarding **exits non-zero** and says exactly what's wrong if the token
is rejected, the identity didn't resolve, or no tools registered. It never "succeeds" while broken.

## Use it

The bot prints **"online as <name> — DM me or @mention me in a channel"**. Then:

- **DM** the bot, or
- **@mention** it in a channel: `@YourBot what can you help me with?`

Each message runs one agent turn — inference, tools, memory, your identity. Replies are split to
Discord's 2000-character cap.

## Step 3 — customize behavior

Edit the pack, not the bot, then re-run onboarding:

- **`identity.yaml`** — `description`, `core_trait`, `drive`, `temperament`, `skills`. This is the
  personality and the system prompt.
- **`skills/`** — add methodology docs the agent can follow.
- **Tools** — add `@tool` functions to the pack's `tools/` module and pass
  `--tools-module pack.tools.shop` (or the equivalent) so they register.
- **Inference** — point the agent at a different model/endpoint in `agent.yaml` (e.g. your local
  llama.cpp/vLLM server) — the bot uses whatever the agent uses.

## The two client paths (and why you don't need a paid tier)

- **Built-in adapter:** awdk ships a `DiscordAdapter` (`adk.channels`) with mention/DM handling
  and chunking built in. It is gated to the **`channels`** capability (a paid tier) — onboarding
  tries it first.
- **Hand-rolled client:** if the adapter isn't available or isn't entitled, onboarding falls back to a
  ~20-line `discord.py` client that calls `agent.chat()` directly. **This works on any tier** —
  onboarding is never blocked on a license.

## Standalone alternative (no `adk onboard` yet)

If you're on an older awdk without `--discord`, the same flow ships as a standalone launcher:

```bash
git clone https://github.com/Aitherium/awskills && cd awskills
python tools/discord-agent-bot.py --check --identity <your-agent>   # the gate
DISCORD_BOT_TOKEN=<token> python tools/discord-agent-bot.py --identity <your-agent>
```

## Security

- The bot token is a **secret**: pass it via `DISCORD_BOT_TOKEN` or `--token`, never commit it, and
  use the minimal bot scope. Reset it in the Developer Portal if it ever leaks.
- The agent runs with the same privileges you gave your awdk setup — don't point it at Discord
  channels you wouldn't let it act on.

## Troubleshooting

- **`discord.py required`** → `pip install 'awdk[channels]'`.
- **Onboarding says the token is invalid/revoked** → reset it in the Developer Portal, re-export
  `DISCORD_BOT_TOKEN`, re-run. The onboarding never proceeds past a bad token.
- **`identity did not resolve`** → the pack installed but the identity file isn't where the agent
  looks; check `~/.aither/agents/<name>/identity.yaml` and re-run with the right `--identity`.
- **Bot doesn't reply in a channel but works in DMs** → the Message Content intent is off, or the
  bot isn't being @mentioned (it only answers DMs and mentions).
- **Reply cut off** → replies are chunked at 2000 chars; if a single tool result is larger, the
  agent's answer itself may need a `--tools-module` that summarizes.

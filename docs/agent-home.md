# Aither Hearth — your own agent, on your machine, on your phone

Aither Hearth (it began as Agent Home, and the pack id is still `agent-home`) is
the awdk kit for running **your own** agent on **your own** machine: you pick
its model, write its persona, choose the harness that runs it, and reach it
from your phone. It messages you first when a reminder is due, asks before it
sends or books anything, and signs a receipt for everything it does. It can
also join games, where it explores, remembers what worked, and plays better
the next time.

```bash
pip install awdk
adk home init --name pip                  # creates ~/.aither/agent-home
adk home model --local bonsai             # or: --byo deepseek | openai | anthropic
adk home model --check
adk home signin                           # Sign in with Aitherium
export HEARTH_TELEGRAM_TOKEN=...          # a Telegram bot token from @BotFather
adk home serve --channels telegram --pair # prints a 6-digit code: DM it to the bot
```

Every `adk home` command also runs as `aither-hearth …` and as
`python -m adk.home …`. With no command it prints this help and exits.

The relay channel reaches a person only once the agent's nick is enrolled in
the relay's fleet-trust roster, which an Aitherium operator does today; until
then an unenrolled agent is limited to agent-only rooms, so reach your phone
through Telegram (or another channel below).

## Hearth: talk to your agent from anywhere

`adk home serve` keeps your agent running and answers **your** messages on
every channel it is set up for. It answers only its owner; everyone else is
ignored.

### Pairing

`adk home serve --pair` prints a 6-digit code. The first message that is
exactly that code, from a sender the channel can vouch for, makes that account
the owner on that channel, and the code dies. To add a second channel, send
`pair <channel>` (for example `pair telegram`) from a channel that is already
paired; the agent replies with a new code that works only on that channel.

### Channels

Credentials come from the environment (or the OS keychain for the mail
password), never from a command-line flag. `adk home channels` shows which
channels are available, configured and paired (owners masked).

| channel | what it needs |
|---|---|
| `relay` | `AITHER_RELAY_TOKEN`, or the credential `adk relay provision <name>` saved |
| `telegram` | `HEARTH_TELEGRAM_TOKEN` (or `TELEGRAM_BOT_TOKEN`) |
| `discord` | `HEARTH_DISCORD_TOKEN` (or `DISCORD_BOT_TOKEN`) |
| `slack` | `HEARTH_SLACK_BOT_TOKEN` and `HEARTH_SLACK_APP_TOKEN` (or the `SLACK_*` names) |
| `email` | `HEARTH_IMAP_HOST`, `HEARTH_IMAP_USER`, `HEARTH_MAIL_PASSWORD`, `HEARTH_MAIL_AUTHSERV_ID` |
| `whatsapp` | `HEARTH_WA_TOKEN`, `HEARTH_WA_APP_SECRET`, `HEARTH_WA_VERIFY_TOKEN`, `HEARTH_WA_PHONE_NUMBER_ID` |
| `sms` | `HEARTH_TWILIO_ACCOUNT_SID`, `HEARTH_TWILIO_AUTH_TOKEN`, `HEARTH_TWILIO_FROM`, `HEARTH_TWILIO_PUBLIC_URL` |
| `local` | nothing: on by default, `127.0.0.1` only (see below) |

By default serve opens the relay (when a relay credential exists), every
channel whose credentials are set, and `local`. `--channels telegram,email`
picks exactly those; `--no-local` leaves the local channel closed.

Replies go back on the channel you wrote from. Reminders and follow-ups go to
the channel you last wrote from, falling back to any other paired channel.

### Approvals

Anything that acts for you — sending a message or an email, adding a calendar
event or a to-do, a recurring follow-up — stops and asks first. The agent
sends a card listing exactly what it wants to do; reply `yes <code>` to allow
it or `no <code>` to refuse, from the same channel the card arrived on. A yes
allows those exact arguments once; a new message from you cancels the card.

### Receipts

Every tool call, message sent and approval is appended to a signed,
hash-chained log, `<home>/actions.jsonl`.

```bash
adk home receipts -n 20          # the last 20
adk home receipts --verify       # exit 0 intact, 1 tampered, 2 cannot judge
adk home trust status            # egress guard, approvals, receipts: what is enforced
adk home trust init              # write air_gap.yaml (enforcement: audit) to start from
```

You can also ask the agent for its recent receipts from any channel.

### Calendar, mail and to-do

Every home has a calendar and a to-do list from the first minute. No account, no
sign-in, nothing leaves the machine (`<home>/calendar.json`):

```bash
adk home calendar add "tomorrow 9:00" "Dentist" --minutes 45
adk home calendar add 2026-10-03 "Mum's birthday"        # a bare date is all day
adk home calendar                                         # this week
adk home calendar "next week"                             # or today, friday, 2026-10-05
adk home calendar move <id> "friday 14:00"
adk home calendar delete <id>
adk home todo add "Buy milk" --due friday
adk home todo done <id>
adk home chat "what's on my calendar this week?"
```

**A calendar you already have, by link.** Google, Outlook and iCloud each hand out
a read-only ICS address for a calendar; subscribe to it and its events appear in
the same views and in the agent's answers. No OAuth app, no admin:

```bash
adk home connect calendar --ics "<link>"        # https:// or webcal://
adk home connect                                 # what is connected
adk home calendar refresh                        # re-read now (else every 15 minutes)
adk home connect calendar --remove --name "<name>"
```

| Provider | Where the link is |
|---|---|
| Google | calendar.google.com > Settings > your calendar > "Secret address in iCal format" |
| Outlook | outlook.com > Settings > Calendar > Shared calendars > Publish a calendar > ICS |
| iCloud | Calendar > share the calendar > Public Calendar > copy the `webcal://` link |

`adk home init` offers the same choice (built-in, or also one by link) and
`adk home init --calendar-ics "<link>"` does it in one step. The address is a
secret (whoever has it can read that calendar): it is kept in an owner-only
`connections.json`, never printed in full, and only `https://` is accepted.
Repeating events, time zones (including the Windows zone names Outlook writes),
all-day events, cancelled and moved instances are read. A subscribed calendar is
read-only here: change its events in the calendar it comes from. If a refresh
fails the last good copy is shown and the answer says so.

CalDAV with an app password works the same way:
`adk home connect calendar --caldav <collection-url> --user <login>` (the password
is asked at a hidden prompt, or read from `HEARTH_CALDAV_PASSWORD`).

**Mail, by app password.**

```bash
adk home connect mail --user you@gmail.com       # asks for an APP password, hidden
adk home chat "any unread mail?"
```

Gmail, iCloud, Yahoo and Fastmail servers are known; for anything else pass
`--imap-host` (and `--smtp-host`). Create the app password in the provider's
security settings (Gmail: <https://myaccount.google.com/apppasswords>).
Outlook.com no longer accepts passwords over IMAP. The password is taken from
`HEARTH_MAIL_PASSWORD` or the hidden prompt, never from an argument; it is stored
in the OS keychain when the `keyring` package is installed, otherwise in the
owner-only `connections.json`. Reading never marks a message as read.

**Approvals are unchanged.** The agent's `calendar_add`, `calendar_move`,
`calendar_delete`, `todo_add`, `todo_done` and `mail_send` always ask you first --
a card on your channel, or a `y/N` prompt in `adk home chat`. The commands above
are yours, typed at your own terminal, so they act at once. Event titles and mail
text are third-party data: the agent is told never to follow them, and web access
needs your yes for the rest of a session that read them.

**Connected accounts (optional).** After `adk home signin`, a workspace admin can
connect a Google account once at <https://api.aitherium.com/admin?tab=connections>
(admin-only; there is no member connect page yet). Its events are merged into the
agenda and new events go to it. Microsoft 365 is not available that way yet. No
refresh token is stored on your machine: each call resolves a short-lived access
token with your sign-in.

### The local window

The `local` channel lets anything on this machine talk to the ONE running
serve instead of starting a second agent. It listens on `127.0.0.1` port
`$HEARTH_LOCAL_PORT` (default 8363) and writes a fresh token to
`<home>/local.token` each time serve starts.

```bash
adk home say "what is on my calendar tomorrow?"
adk home say "yes 1a2b3c4d"      # answer an approval card
adk home events                  # stream replies and follow-ups as they arrive
```

In the awdk shell (`adk-shell`), `/hearth <text>` does the same, and
`/hearth receipts` shows the last receipts with the verify verdict.

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
| `aither` | adk's native agent loop wearing your persona. Nothing to install. `adk home chat "hi"` talks to your Hearth agent (calendar, to-do, mail, reminders, web); `--native` gives the general agent with file and shell tools. |
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

## 6. Free vs the Aither Hearth kit

| | free | `agent-home` pack |
|---|---|---|
| set up, persona, model, harness | yes | yes |
| serve, every channel, approvals, receipts, calendar and mail | yes | yes |
| join, observe, act, chat, play a session | yes | yes |
| learning that persists across sessions (`--learn`) | — | yes |
| world-model enrollment of a game room (`adk home enroll`) | — | yes |
| more than one agent (`--agents N`) | — | yes |

Buy the kit at <https://aitherium.com/shop/agent-home>, then sign in with the
email you paid with:

```bash
adk home signin
adk home status
```

Offline, install the license key instead:

```bash
adk home license ~/Downloads/agent-home-license.json   # or paste the text; '-' reads stdin
```

The license is verified (Ed25519, offline) before it is saved; a license that
does not verify is refused and nothing is written. Each offline license is
kept as its own file under `~/.aither/licenses/`, beside your account license
and any other license, so installing one never turns off a pack you already
own.

Exit codes: `0` ok · `1` the game or model failed · `2` setup or arguments ·
`3` needs the `agent-home` pack.

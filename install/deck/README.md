# Aither on a Steam Deck

Add a Steam Deck to your Aither fleet without touching SteamOS: no sudo, no
developer mode, the read-only root stays read-only, and games always come first.

## Install (Desktop Mode)

Open Konsole and paste one line:

```bash
curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/install/deck/deck-install.sh | bash
```

Or download [`join-aither-fleet.desktop`](join-aither-fleet.desktop) and double-click it.

A sign-in code appears. Open the link on your phone, type the code, and the Deck
enrols itself in your workspace. Check it with `adk devices list`.

## What it installs (all under your home directory)

| piece | where | what it is for |
|---|---|---|
| uv | `~/.local/bin/uv` | user-level Python; SteamOS's Python is never touched |
| awdk (`adk`) | uv tool | the agent toolkit |
| awsh | `~/.local/share/aither-deck/` + private Node.js (sha256-checked) | the terminal assistant |
| `aither-deck-node` | systemd --user | `adk rc`: enrolment, heartbeat, the reverse link |
| `aither-deck-shell` | systemd --user | `adk harness serve` on 127.0.0.1 only |
| `aither-deck-guard` | systemd --user | games first: see below |
| `aither-deck-holder` | systemd --user, guard-controlled | `adk kvholder serve`: lend memory to the fleet |

Every service runs at idle CPU and I/O priority.

## Games first

`deck-guard.sh` checks every 10 s. Steam starts every game under
`reaper SteamLaunch AppId=<id>`; while one runs, the guard stops the session daemon
and the memory holder and freezes the node agent so it uses no CPU. When the game
exits, they resume. What it saw is in `~/.local/state/aither-deck/presence.json`.

Memory is lent only when all of these hold: you installed with `--lend-memory`
and set `DECK_HOLDER_CONNECT` in `~/.config/aither-deck/deck.env`, the Deck is on
AC power, it is docked (an external display; `--no-dock-required` drops this),
and no game is running.

`~/.local/share/aither-deck/deck-guard.sh --self-test` proves each check can say
both yes and no.

## Remove

```bash
~/.local/share/aither-deck/deck-uninstall.sh
```

or the "Leave Aither fleet" launcher. It removes the Deck from your workspace,
the services, awsh, Node.js, awdk, and uv and `~/.aither` when the installer
created them. `--keep-enrollment` leaves the device listed in your workspace.

## Options

`deck-install.sh --help` lists them: `--no-awsh`, `--no-enroll`, `--lend-memory`,
`--holder-max-mb N` (default 6144), `--no-dock-required`, `--api-key-file F`
(headless sign-in), `--awdk-spec S`.

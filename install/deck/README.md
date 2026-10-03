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

Already signed in on another screen? Mint a pairing code there ("Add a laptop or
Steam Deck") and pair instead of signing in on the Deck:

```bash
curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/install/deck/deck-install.sh | bash -s -- --pair CODE
```

Codes live 5 minutes.

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
| `aither-deck-rpc` | systemd --user, guard-controlled, opt-in | a ggml-rpc worker: lend compute to the pool |

Every service runs at idle CPU and I/O priority.

## Games first

`deck-guard.sh` checks every 10 s. Steam starts every game under
`reaper SteamLaunch AppId=<id>`; while one runs, the guard stops the session daemon
and the memory holder and freezes the node agent so it uses no CPU. When the game
exits, they resume. What it saw is in `~/.local/state/aither-deck/presence.json`.

Memory is lent only when all of these hold: you installed with `--lend-memory`
and the holder has a way in (mesh discovery, on by default when your `adk`
supports `kvholder serve --mesh`, or a relay URL in `DECK_HOLDER_CONNECT` in
`~/.config/aither-deck/deck.env`), the Deck is on AC power, it is docked (an
external display; `--no-dock-required` drops this), and no game is running.

With an `adk` that has `kvholder serve --device`, the holder dials the workspace
relay (`wss://kv.aitherium.com/holder`) signed with this Deck's enrolled device key;
no token. Allow it once from any signed-in machine:
`adk kvholder workspace allow <node id>` (the id is in `adk devices list`).

A mesh join prints a 6-letter code (`journalctl --user -u aither-deck-holder`);
approve it from your desktop with `adk kvholder mesh approve CODE`.

Compute is lent only with `--lend-compute` (plus a prebuilt worker, its sha256 and a
private `--rpc-bind` address), while docked, on AC power and with no game running; the
dock is always required for compute and the guard stops the worker the moment a game starts.

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
`--holder-max-mb N` (default 6144), `--no-dock-required`, `--lend-compute`,
`--rpc-worker-bin F`, `--rpc-worker-sha256 H`, `--rpc-bind ADDR`, `--rpc-max-mb N`,
`--api-key-file F`
(headless sign-in), `--pair CODE` (enrol with a pairing code), `--awdk-spec S`.

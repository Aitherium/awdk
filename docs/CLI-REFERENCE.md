<!-- GENERATED FILE — DO NOT EDIT BY HAND.

Produced by `scripts/gen_cli_reference.py` from the live argparse tree, which is
the same introspection AitherShell uses for slash commands. Edit the parser in
`adk/cli.py` and regenerate:

    python scripts/gen_cli_reference.py

A hand edit here is reverted by the next run, and CI diffs this file against a
fresh generation.
-->

# `adk` command reference

Every top-level command, generated from the parser itself — so this cannot
describe a command that does not exist, and cannot omit one that does.

Run `adk <command> --help` for the authoritative, always-current detail.


**97 commands.**

| command | what it does |
|---|---|
| [`adk acp`](#adk-acp) | Agent Client Protocol: serve an agent to ACP editors, or drive an external ACP agent |
| [`adk addon`](#adk-addon) | Manage self-hosted service addons (Qdrant, RAG, CodeGraph, etc.) |
| [`adk admin`](#adk-admin) | Administration commands |
| [`adk aeon`](#adk-aeon) | Multi-agent group chat |
| [`adk agent`](#adk-agent) | Run/manage host-tier agent loops (run, list, status, stop) |
| [`adk agent-prompt`](#adk-agent-prompt) | Print the setup prompt for AI coding agents |
| [`adk agents`](#adk-agents) | Discover agents in the mesh (ls) |
| [`adk ambient`](#adk-ambient) | Make the agent an expert on what you're doing in this terminal |
| [`adk approvals`](#adk-approvals) | List/approve/deny A2A permission cards blocking federated agents |
| [`adk backend`](#adk-backend) | Manage LLM backends (list, set, test, switch, status) |
| [`adk backup`](#adk-backup) | Backup all agent data (memory, graphs, config) |
| [`adk balance`](#adk-balance) | Show your Aitherium credit balance and earnings |
| [`adk bonsai-local`](#adk-bonsai-local) | Run Bonsai-27B on your own hardware (:8090) — GPU or CPU; aitherium.com then chats locally |
| [`adk chat`](#adk-chat) | Chat with a mesh agent by name (adk chat <agent> [msg]) |
| [`adk claude`](#adk-claude) | Run scoped headless Claude Code subagents (serve/spawn/runs/kill) |
| [`adk claude-account`](#adk-claude-account) | Manage multiple Claude Code (Anthropic) account profiles |
| [`adk claude-model`](#adk-claude-model) | Switch Claude Code between DeepSeek, Kimi, local AitherOS models, and Anthropic |
| [`adk connect`](#adk-connect) | Connect to AitherOS — detect LLMs, set up gateway, or join desktop mesh |
| [`adk contribute`](#adk-contribute) | Teach Aither's ARC world model — enroll, then play & stream transitions (free) |
| [`adk costs`](#adk-costs) | Show cloud inference costs, savings, and budget |
| [`adk create-app`](#adk-create-app) | Scaffold a awkit workspace app |
| [`adk cron`](#adk-cron) | Manage scheduled tasks |
| [`adk decide`](#adk-decide) | Decision cards — raise a structured ask, list what is waiting, answer it |
| [`adk deploy`](#adk-deploy) | Deploy AitherOS components or agents |
| [`adk disconnect`](#adk-disconnect) | Disconnect from desktop AitherOS mesh |
| [`adk doc`](#adk-doc) | Manage encrypted documents (upload, list, download, delete) |
| [`adk doctor`](#adk-doctor) | Check system health (Python, GPU, LLM backends, API keys) |
| [`adk down`](#adk-down) | Stop the agent + tunnel and remove its autostart |
| [`adk enroll`](#adk-enroll) | Register this workstation with the control plane |
| [`adk eval`](#adk-eval) | Evaluate MCP tools and packs on a connected gateway |
| [`adk explore`](#adk-explore) | Browse packs, agents, and skills in the Aitherium marketplace |
| [`adk fleet`](#adk-fleet) | Create & manage a fleet of agents (local \| managed \| cloud-run) |
| [`adk forge`](#adk-forge) | Dispatch tasks to agent forge (Genesis) |
| [`adk gateway`](#adk-gateway) | Run agent across messaging platforms |
| [`adk gobbonet`](#adk-gobbonet) | Run GobboNet with keyless web search (clones the UI if needed) |
| [`adk graph`](#adk-graph) | Provenance graph management (status, drain, claim, ground, context, leaves, lineage, runs, show, purge) |
| [`adk grid`](#adk-grid) | Manage grid distributed nodes (add, remove, list, test, sync) |
| [`adk harness`](#adk-harness) | AitherShell — drive Claude Code, other coding harnesses, agents and real terminals |
| [`adk host`](#adk-host) | Host a self-hosted agent (your model key) + connect it to your fleet — one command |
| [`adk image`](#adk-image) | Generate an image on a local backend (ComfyUI/Sana/SD.Next) |
| [`adk index`](#adk-index) | Index a codebase for code search (CodeGraph) |
| [`adk ingest`](#adk-ingest) | Ingest files into the agent's knowledge graph |
| [`adk init`](#adk-init) | Scaffold a new agent project |
| [`adk install`](#adk-install) | Install an agent pack (e.g. adk install pack:openclaw) |
| [`adk integrate`](#adk-integrate) | Connect external tools (OpenClaw, etc.) |
| [`adk invoke`](#adk-invoke) | Invoke a tool on a mesh agent over signed A2A (adk invoke <agent> <skill>) |
| [`adk jobs`](#adk-jobs) | Manage background jobs — LOCAL by default, --remote for the portal/cloud |
| [`adk join`](#adk-join) | One-command community node onboarding (GitHub auth + hardware detection + serve + mesh join + earnings) |
| [`adk keys`](#adk-keys) | Manage cloud provider API keys (set, list, test, remove) |
| [`adk listen`](#adk-listen) | Real-time audio intelligence — audiobook, meeting, voice notes |
| [`adk login`](#adk-login) | Authenticate with Aitherium (browser device flow) |
| [`adk logout`](#adk-logout) | Clear saved auth tokens |
| [`adk mcp`](#adk-mcp) | MCP server, IDE setup, and cloud gateway connection |
| [`adk mesh`](#adk-mesh) | AitherMesh overlay operations (onboard, list peers) |
| [`adk new`](#adk-new) | Scaffold a full template app (e.g. deep-research) |
| [`adk notebook`](#adk-notebook) | Plan, run, and inspect Agent Notebooks (.anb) on Genesis |
| [`adk onboard`](#adk-onboard) | Interactive onboarding — detect, configure, integrate |
| [`adk pack`](#adk-pack) | Manage ToolPack extensions (list, search, install, remove, info) |
| [`adk packs`](#adk-packs) | List available agent packs |
| [`adk pair`](#adk-pair) | Pair this machine with the portal as an inference node (6-char code from the portal) |
| [`adk platform`](#adk-platform) | Internal platform toolkit (merged from aither-platform) |
| [`adk publish`](#adk-publish) | Publish agent to Elysium marketplace |
| [`adk publish-preflight`](#adk-publish-preflight) | Check a package can actually be published: an interpreter that meets requires-python, and a wheel that installs AND imports |
| [`adk quickstart`](#adk-quickstart) | One-command setup: GPU + auth + shell |
| [`adk quickstart-local`](#adk-quickstart-local) | Local inference quickstart (no cloud required) |
| [`adk register`](#adk-register) | Create a new Aitherium account |
| [`adk relay`](#adk-relay) | Connect this agent to AitherRelay chat (join + serve DMs) |
| [`adk reregister`](#adk-reregister) | Re-register endpoint(s) with A2A public keys (backfill for existing endpoints) |
| [`adk routing`](#adk-routing) | Manage per-intent model routing (which model handles which task) |
| [`adk run`](#adk-run) | Start the agent server |
| [`adk sandbox`](#adk-sandbox) | Self-host AitherSandbox + link it to your portal (optional safe-testing) |
| [`adk secret`](#adk-secret) | Manage secrets (list, get, set, pull, push, sync) |
| [`adk setup`](#adk-setup) | Interactive GPU setup wizard (vLLM/Ollama) + optional AitherOS stack |
| [`adk setup-all`](#adk-setup-all) | Install/set up all AitherOS client products (adk + shell + node + connect) |
| [`adk shell`](#adk-shell) | Launch AitherShell interactive terminal |
| [`adk skills`](#adk-skills) | Manage learned skills |
| [`adk soul`](#adk-soul) | Import/export SOUL.md identity files |
| [`adk ssh`](#adk-ssh) | Open a remote terminal into a prod/dev environment via the tunnel |
| [`adk ssh-cert`](#adk-ssh-cert) | Fetch a short-lived SSH certificate from the AitherCert SSH CA (GitHub org SSH) |
| [`adk stack`](#adk-stack) | Start the consumer stack (Room + Ollama) as native processes |
| [`adk start`](#adk-start) | Start chatting with your codebase (zero config) |
| [`adk status`](#adk-status) | Show backend and service status |
| [`adk support`](#adk-support) | Get help — Discord, GitHub, docs |
| [`adk sync`](#adk-sync) | Sync local directory with AitherOS platform |
| [`adk test`](#adk-test) | Run agent tests |
| [`adk tools`](#adk-tools) | Manage available tools (list, sync from platform) |
| [`adk train`](#adk-train) | Manage model training (launch, monitor, cancel) |
| [`adk ui`](#adk-ui) | Manage the agent's web UI pack (ls / set / path) |
| [`adk up`](#adk-up) | Run a persistent agent connected to your AitherOS fleet (one command) |
| [`adk upgrade`](#adk-upgrade) | Open upgrade/checkout page for a pack or plan |
| [`adk vault`](#adk-vault) | Lockbox for the live secrets vault (setup, ls, get, search, rotate, lock) |
| [`adk voice`](#adk-voice) | Voice services (serve standalone HTTP server) |
| [`adk whoami`](#adk-whoami) | Show current auth status, config and entitlement tier |
| [`adk wizard`](#adk-wizard) | First-run wizard — hardware detection, setup recommendations, auth token |
| [`adk wm`](#adk-wm) | World model management (status, inspect, train, reset) |
| [`adk workspace`](#adk-workspace) | Manage dev workspaces on AitherOS tunnel |
| [`adk x-session`](#adk-x-session) | Bootstrap the autonomous X poster's logged-in session |

---

## `adk acp`

Agent Client Protocol: serve an agent to ACP editors, or drive an external ACP agent

**Subcommands**

- `adk acp serve` — Serve an AitherOS agent over ACP stdio (the editor-facing entrypoint)
- `adk acp login` — Interactive AitherIdentity sign-in (ACP Terminal Auth entrypoint)
- `adk acp connect` — Connect to an external ACP agent and report its identity
- `adk acp prompt` — Prompt an external ACP agent once and print its reply
- `adk acp list-sessions` — List sessions of an external ACP agent
- `adk acp config` — Emit editor config that runs `adk acp serve` as an ACP agent

## `adk addon`

Manage self-hosted service addons (Qdrant, RAG, CodeGraph, etc.)

**Subcommands**

- `adk addon list` — Show available addons + status
- `adk addon enable` — Pull image, start container, register with portal
- `adk addon disable` — Stop container, deregister
- `adk addon status` — Health + metrics for addons
- `adk addon logs` — Tail container logs
- `adk addon update` — Pull latest images for all enabled addons

## `adk admin`

Administration commands

**Subcommands**

- `adk admin create-token` — Create a node token on the desktop for mesh enrollment

## `adk aeon`

Multi-agent group chat

| option | type | required | default | description |
|---|---|---|---|---|
| `-p`, `--preset` | str |  |  | Preset: balanced, creative, technical, security, minimal, duo_code, research |
| `-a`, `--agents` | str |  |  | Comma-separated agent names (e.g. demiurge,athena) |
| `-r`, `--rounds` | int |  | `1` | Discussion rounds per message (default: 1) |
| `--no-synthesize` | str |  | `false` | Skip orchestrator synthesis |

## `adk agent`

Run/manage host-tier agent loops (run, list, status, stop)

**Subcommands**

- `adk agent run` — Start a host-tier agent loop
- `adk agent list` — List running agent loops
- `adk agent status` — Show status of an agent loop
- `adk agent stop` — Stop a running agent loop

## `adk agent-prompt`

Print the setup prompt for AI coding agents

| option | type | required | default | description |
|---|---|---|---|---|
| `--raw` | str |  | `false` | Print raw prompt without footer |

## `adk agents`

Discover agents in the mesh (ls)

**Subcommands**

- `adk agents ls` — List every agent in the mesh + its inference backend

## `adk ambient`

Make the agent an expert on what you're doing in this terminal

**Subcommands**

- `adk ambient install` — Add the shell hook to your profile (opt-in)
- `adk ambient uninstall` — Remove the shell hook
- `adk ambient report` — Report one finished command (called by the shell hook)
- `adk ambient brief` — What does the agent already know about this?
- `adk ambient status` — Show ambient loop stats and engine health

## `adk approvals`

List/approve/deny A2A permission cards blocking federated agents

**Subcommands**

- `adk approvals list` — Show pending permission cards
- `adk approvals approve` — Approve a card and mint its one-time grant token
- `adk approvals deny` — Refuse a card; no token is minted

| option | type | required | default | description |
|---|---|---|---|---|
| `--url` | str |  |  | A2A gateway base URL (default $AITHER_A2A_URL or https://127.0.0.1:8766) |
| `--json` | str |  | `false` | Emit raw JSON |

## `adk backend`

Manage LLM backends (list, set, test, switch, status)

**Subcommands**

- `adk backend list` — Show detected and configured backends
- `adk backend guide` — Step-by-step setup guide for a backend (no arg = menu)
- `adk backend set` — Set default backend
- `adk backend add` — Register a custom backend (acp: drive an external ACP agent)
- `adk backend set-reasoning` — Set reasoning-only backend (effort 7+)
- `adk backend test` — Test current backend with a simple prompt
- `adk backend switch` — Switch to a different inference backend
- `adk backend use` — Switch the RUNNING agent live to a preset (no restart)
- `adk backend status` — Show current backend configuration and connectivity

## `adk backup`

Backup all agent data (memory, graphs, config)

| option | type | required | default | description |
|---|---|---|---|---|
| `-o`, `--output` | str |  |  | Output file path (default: aither-backup-<timestamp>.tar.gz) |

## `adk balance`

Show your Aitherium credit balance and earnings

## `adk bonsai-local`

Run Bonsai-27B on your own hardware (:8090) — GPU or CPU; aitherium.com then chats locally

| option | type | required | default | description |
|---|---|---|---|---|
| `--port` | int |  | `8090` | Host port to serve on (default: 8090) |
| `--dry-run` | str |  | `false` | Show what would run without starting anything |
| `--stop` | str |  | `false` | Stop and remove the local Bonsai container |

## `adk chat`

Chat with a mesh agent by name (adk chat <agent> [msg])

| option | type | required | default | description |
|---|---|---|---|---|
| `<agent>` | str | yes |  | Agent name from `adk agents ls` |
| `<message>` | str |  |  | Message (omit for an interactive loop) |

## `adk claude`

Run scoped headless Claude Code subagents (serve/spawn/runs/kill)

**Subcommands**

- `adk claude serve` — Run the subagent runner daemon
- `adk claude spawn` — Spawn a scoped subagent run
- `adk claude runs` — List subagent runs
- `adk claude kill` — Cancel a run

## `adk claude-account`

Manage multiple Claude Code (Anthropic) account profiles

**Subcommands**

- `adk claude-account save` — Save current Claude Code login
- `adk claude-account list` — List saved profiles
- `adk claude-account switch` — Switch to a saved profile
- `adk claude-account current` — Show current profile name
- `adk claude-account remove` — Delete a saved profile
- `adk claude-account usage` — Show multi-account usage and scheduling status

## `adk claude-model`

Switch Claude Code between DeepSeek, Kimi, local AitherOS models, and Anthropic

**Subcommands**

- `adk claude-model list` — Show available model profiles
- `adk claude-model use` — Switch Claude Code to a profile
- `adk claude-model status` — Show the active profile
- `adk claude-model check` — Prove the active profile answers a real turn
- `adk claude-model bridge` — Manage the translation bridge
- `adk claude-model auto` — One-shot: start bridge + switch + verify
- `adk claude-model failover` — Test current; if broken, switch to next working provider
- `adk claude-model watch` — Auto-switch on rate limit (daemon)
- `adk claude-model plan` — → Anthropic Opus 5 (architecture, design, review)
- `adk claude-model code` — → DeepSeek Flash (fast ultracode, 1M context)
- `adk claude-model reason` — → DeepSeek Pro (deep reasoning, 1M context)
- `adk claude-model kimi` — → Kimi K3 (1M context, thinking always on)
- `adk claude-model local` — → qwen3.6-27b on DGX
- `adk claude-model fast` — → gemma4-12b for trivial tasks

## `adk connect`

Connect to AitherOS — detect LLMs, set up gateway, or join desktop mesh

| option | type | required | default | description |
|---|---|---|---|---|
| `--api-key` | str |  |  | AITHER_API_KEY for cloud inference |
| `--elysium` | str |  |  | Connect to desktop AitherOS (e.g. http://192.168.1.10:8001) |
| `--token` | str |  |  | Node token for desktop mesh authentication |
| `--save` | str |  | `true` | Save config to ~/.aither/config.json (default: true) |
| `--no-save` | str |  | `true` | Don't save config |

## `adk contribute`

Teach Aither's ARC world model — enroll, then play & stream transitions (free)

**Subcommands**

- `adk contribute register` — Mint a free wallet + contributor token (idempotent)
- `adk contribute play` — Play ARC games and stream every transition (needs: pip install 'awdk[arc]')
- `adk contribute status` — Your accepted count + daily quota
- `adk contribute leaderboard` — Who has taught it the most
- `adk contribute solo` — Print the one-command self-host (train YOUR own model)

## `adk costs`

Show cloud inference costs, savings, and budget

**Subcommands**

- `adk costs summary` — Show cost summary (default)
- `adk costs compare` — Compare AitherOS vs raw API costs
- `adk costs budget` — Set monthly spending budget

| option | type | required | default | description |
|---|---|---|---|---|
| `--period` | str |  | `day` | Time period |

## `adk create-app`

Scaffold a awkit workspace app

| option | type | required | default | description |
|---|---|---|---|---|
| `<name>` | str | yes |  | App name (e.g. 'ACME Assistant') |
| `-o`, `--output` | str |  |  | Output directory (default: ./<slug>) |
| `--company` | str |  |  | Company name |
| `--industry` | str |  | `general` | Industry vertical |
| `--description` | str |  |  | What this app does |
| `--subdomain` | str |  |  | URL slug (auto-derived from name) |
| `--color` | str |  | `#6366f1` | Primary brand color |
| `--template` | str |  | `default` | Base template (default: default) |
| `--llm-provider` | str |  | `aitheros` | LLM provider (default: aitheros) |
| `--force` | str |  | `false` | Overwrite existing directory |

## `adk cron`

Manage scheduled tasks

**Subcommands**

- `adk cron list` — List scheduled jobs
- `adk cron add` — Add a cron job
- `adk cron remove` — Remove a cron job

## `adk decide`

Decision cards — raise a structured ask, list what is waiting, answer it

| option | type | required | default | description |
|---|---|---|---|---|
| `<decide_args>` | str |  |  | ask \| list \| show \| answer \| cancel \| watch \| sweep |

## `adk deploy`

Deploy AitherOS components or agents

**Subcommands**

- `adk deploy ollama` — Install Ollama + pull models for your GPU
- `adk deploy vllm` — Deploy vLLM containers directly (use 'adk setup' for guided wizard)
- `adk deploy node` — Deploy agent node (default: ADK-native lightweight; --genesis for full stack)
- `adk deploy core` — Core services (Node, Pulse, Watch, Genesis, Veil)
- `adk deploy full` — Full AitherOS stack (~31 containers)
- `adk deploy fleet-refresh` — Rebuild all lib-baking Python images + safe rolling recreate of the running fleet
- `adk deploy addons` — Deploy self-hosted addon services
- `adk deploy connect` — Awconnect browser extension
- `adk deploy desktop` — AitherDesktop native application
- `adk deploy grid` — Deploy grid distributed stack (GPU + Mac + cluster)
- `adk deploy stop` — Stop a running deployment
- `adk deploy agent` — Deploy a tenant agent to this machine (or upload to gateway)

## `adk disconnect`

Disconnect from desktop AitherOS mesh

## `adk doc`

Manage encrypted documents (upload, list, download, delete)

**Subcommands**

- `adk doc upload` — Upload and encrypt a document
- `adk doc list` — List your encrypted documents
- `adk doc get` — Download and decrypt a document
- `adk doc delete` — Delete a document

## `adk doctor`

Check system health (Python, GPU, LLM backends, API keys)

## `adk down`

Stop the agent + tunnel and remove its autostart

| option | type | required | default | description |
|---|---|---|---|---|
| `--keep-autostart` | str |  | `false` | Stop now but leave the reboot-autostart entry in place |

## `adk enroll`

Register this workstation with the control plane

| option | type | required | default | description |
|---|---|---|---|---|
| `--portal` | str |  |  | Portal URL (default: portal.aitherium.com) |
| `--genesis` | str |  |  | Genesis URL (default: localhost:8001) |
| `--no-heartbeat` | str |  | `false` | Skip background heartbeat |
| `--force` | str |  | `false` | Re-enroll even if already registered |

## `adk eval`

Evaluate MCP tools and packs on a connected gateway

**Subcommands**

- `adk eval tools` — Evaluate all available tools
- `adk eval pack` — Evaluate a specific pack's declared tools
- `adk eval self-test` — Run offline self-test (proves the harness can fail)

## `adk explore`

Browse packs, agents, and skills in the Aitherium marketplace

| option | type | required | default | description |
|---|---|---|---|---|
| `<category>` | str |  | `all` | Filter: agents, tools, skills, grid, all (default: all) |
| `--free` | str |  | `false` | Show only free packs |

## `adk fleet`

Create & manage a fleet of agents (local | managed | cloud-run)

**Subcommands**

- `adk fleet create` — Create an agent in a runtime
- `adk fleet list` — List fleet members
- `adk fleet status` — Refresh & show one member's status
- `adk fleet rm` — Remove a member (teardown + drop record)
- `adk fleet connect-local` — Register this machine's local agent MCP endpoint with the gateway (bidirectional)
- `adk fleet apply-pack` — Push+enable a bundled pack on a mesh agent (no SSH; 'self' = this node)

## `adk forge`

Dispatch tasks to agent forge (Genesis)

| option | type | required | default | description |
|---|---|---|---|---|
| `<task>` | str | yes |  | Task description (quoted string) |
| `--agent` | str |  | `demiurge` | Agent to dispatch to (default: demiurge) |
| `--effort` | int |  | `5` | Effort level 1-10 (default: 5) |
| `--watch` | str |  | `true` | Stream progress (default: true) |
| `--no-watch` | str |  | `true` | Don't stream progress |

## `adk gateway`

Run agent across messaging platforms

| option | type | required | default | description |
|---|---|---|---|---|
| `-a`, `--agent` | str |  | `assistant` | Agent identity (default: assistant) |
| `--telegram` | str |  | `false` | Enable Telegram (TELEGRAM_BOT_TOKEN) |
| `--discord` | str |  | `false` | Enable Discord (DISCORD_BOT_TOKEN) |
| `--slack` | str |  | `false` | Enable Slack (SLACK_BOT_TOKEN + SLACK_APP_TOKEN) |
| `--webhook` | str |  | `false` | Enable webhook endpoint |
| `--webhook-port` | int |  | `9000` | Webhook port (default: 9000) |

## `adk gobbonet`

Run GobboNet with keyless web search (clones the UI if needed)

| option | type | required | default | description |
|---|---|---|---|---|
| `--ui` | str |  |  | existing GobboNet checkout (default: find or clone) |
| `--port` | int |  | `11434` |  |
| `--host` | str |  | `127.0.0.1` |  |
| `--no-open` | str |  | `false` | do not open a browser |
| `--setup-model` | str |  | `false` | install llama.cpp + a model sized to this machine |
| `--backend` | str |  |  | pin an OpenAI-compatible server (e.g. http://127.0.0.1:8000) |
| `--plain` | str |  | `false` | passthrough chat instead of the adk agent loop |

## `adk graph`

Provenance graph management (status, drain, claim, ground, context, leaves, lineage, runs, show, purge)

**Subcommands**

- `adk graph status` — Show spool stats + platform health
- `adk graph drain` — Force drain pending spool entries
- `adk graph claim` — Record a claim from the shell
- `adk graph ground` — Check platform grounding of a statement
- `adk graph context` — Fetch bounded context subgraph for a task
- `adk graph leaves` — Fetch leaf/frontier nodes (unexplored)
- `adk graph lineage` — Show ancestry path for a node
- `adk graph runs` — List recent runs from local spool
- `adk graph show` — Show one node with its edges
- `adk graph purge` — Purge old sent entries from spool

## `adk grid`

Manage grid distributed nodes (add, remove, list, test, sync)

**Subcommands**

- `adk grid status` — Show grid topology and health of all nodes
- `adk grid add` — Add a node to the grid
- `adk grid remove` — Remove a node from the grid
- `adk grid test` — Test connectivity to all or specific nodes
- `adk grid sync` — Sync grid config to your Aitherium workspace (requires login)
- `adk grid pull` — Pull grid config from your Aitherium workspace
- `adk grid enroll` — Mint a single-use token to onboard a remote machine as a mesh node
- `adk grid ls` — List enrolled mesh nodes (GPU, memory, containers, status)
- `adk grid deregister` — Remove an enrolled mesh node from the registry

## `adk harness`

AitherShell — drive Claude Code, other coding harnesses, agents and real terminals

**Subcommands**

- `adk harness serve` — Run the harness daemon
- `adk harness harnesses` — What this box can drive
- `adk harness agents` — Sovereign agent roster
- `adk harness profiles` — Model profiles usable per session
- `adk harness list` — Live sessions
- `adk harness new` — Start a session
- `adk harness send` — Send a turn to a session
- `adk harness attach` — Follow a session's event stream
- `adk harness kill` — Stop a session
- `adk harness wrap` — Terminal-resident daemon session (bridge stdin/stdout to daemon)

| option | type | required | default | description |
|---|---|---|---|---|
| `--url` | str |  |  | Harness daemon URL (default 127.0.0.1:8362) |
| `--token` | str |  |  | Daemon bearer token |

## `adk host`

Host a self-hosted agent (your model key) + connect it to your fleet — one command

| option | type | required | default | description |
|---|---|---|---|---|
| `--provider` | str |  |  | Model provider: deepseek/openai/anthropic (prompted if omitted) |
| `--model` | str |  |  | Model name (default: the provider's default) |
| `--name` | str |  |  | Fleet label for this agent (default: <hostname>-adk) |
| `--identity` | str |  | `aither` | Agent identity to load (default: aither) |
| `--port` | int |  | `8080` | Local port for aither-serve (default: 8080) |
| `--approve` | str |  |  | Comma-list of tools that pause for approval, or '*' (default: file_write,shell_exec,shell) |
| `--token` | str |  |  | Control-plane token for registration (else 'adk login' / $AITHER_PORTAL_TOKEN) |
| `--auth-token` | str |  |  | Callback bearer the control plane presents back to your agent (minted if omitted) |
| `--portal` | str |  | `https://veil.aitherium.com` | Control-plane base URL |
| `--login-url` | str |  |  | Device-flow login base URL (default: --portal, then portal.aitherium.com) |
| `--register-url` | str |  |  | Full fleet-register URL (overrides --portal; e.g. http://localhost:8001/v1/agent/fleet/register) |
| `--no-register` | str |  | `false` | Run locally only — no tunnel, no fleet registration |
| `--dry-run` | str |  | `false` | Show what would happen without starting anything |

## `adk image`

Generate an image on a local backend (ComfyUI/Sana/SD.Next)

| option | type | required | default | description |
|---|---|---|---|---|
| `--backends` | str |  | `false` | list which lanes can actually generate |
| `<prompt>` | str |  |  | what to draw |
| `--width` | int |  | `768` |  |
| `--height` | int |  | `768` |  |
| `--steps` | int |  | `20` |  |
| `--cfg` | float |  | `6.0` |  |
| `--seed` | int |  |  |  |
| `--model` | str |  |  | checkpoint to use |
| `--backend` | str |  |  | force a lane id |
| `--negative` | str |  |  |  |
| `--out` | str |  |  | output PNG path |

## `adk index`

Index a codebase for code search (CodeGraph)

| option | type | required | default | description |
|---|---|---|---|---|
| `<path>` | str |  | `.` | Path to index (default: current directory) |
| `--embed` | str |  | `false` | Also generate embeddings for semantic search |
| `--stats` | str |  | `false` | Show Python metrics after indexing |

## `adk ingest`

Ingest files into the agent's knowledge graph

| option | type | required | default | description |
|---|---|---|---|---|
| `<path>` | str |  | `.` | File or directory to ingest |
| `--agent` | str |  | `default` | Agent name for the graph |
| `--brain` | str |  | `false` | Enable sync to CompanyBrain hub (default: local only) |
| `--brain-url` | str |  |  | Override brain hub URL (default: from env/config) |
| `--classification` | str |  | `internal` | Classification level for ingested content (default: internal) |
| `--chunk-size` | int |  | `2000` | Bytes per chunk (default: 2000) |
| `--chunk-overlap` | int |  | `200` | Overlap bytes between chunks (default: 200) |
| `--workspace` | str |  | `default` | Workspace ID for brain sync (default: default) |
| `--skip-embeddings` | str |  | `false` | Skip embedding if brain unreachable |
| `--dry-run` | str |  | `false` | Print what would be ingested without persisting |

## `adk init`

Scaffold a new agent project

| option | type | required | default | description |
|---|---|---|---|---|
| `<name>` | str |  | `my-agent` | Project/agent name |
| `-d`, `--directory` | str |  |  | Target directory (default: ./<name>) |

## `adk install`

Install an agent pack (e.g. adk install pack:openclaw)

| option | type | required | default | description |
|---|---|---|---|---|
| `<target>` | str |  |  | 'list', 'pack:<name>', or a pack name (openclaw, hermes, claude-code) |

## `adk integrate`

Connect external tools (OpenClaw, etc.)

| option | type | required | default | description |
|---|---|---|---|---|
| `<target>` | str |  | `list` | Integration target: openclaw, list |
| `--mode` | str |  |  | Integration mode (default: auto-detect) |
| `--api-key` | str |  |  | AITHER_API_KEY for cloud mode |
| `--dry-run` | str |  | `false` | Show config without writing |
| `--force` | str |  | `false` | Overwrite existing integration config |

## `adk invoke`

Invoke a tool on a mesh agent over signed A2A (adk invoke <agent> <skill>)

| option | type | required | default | description |
|---|---|---|---|---|
| `<agent>` | str | yes |  | Agent name from `adk agents ls` |
| `<skill>` | str | yes |  | Tool/skill name to invoke on the remote agent |
| `--arg` | str |  |  | Tool argument (repeatable); value is JSON-parsed when possible |
| `--as-agent` | str |  |  | Name to sign as (keypair ~/.aither/agent_key.<name>.pem) |

## `adk jobs`

Manage background jobs — LOCAL by default, --remote for the portal/cloud

**Subcommands**

- `adk jobs list` — List jobs (local by default)
- `adk jobs status` — Show status of a job
- `adk jobs steer` — Send a follow-up message to a job
- `adk jobs hint` — Send an invisible hint to a job
- `adk jobs watch` — Watch a cloud job's progress in real-time
- `adk jobs run` — Run a job locally in the FOREGROUND
- `adk jobs start` — Start a local job in the BACKGROUND (detached)
- `adk jobs cancel` — Cancel a running local job
- `adk jobs sync` — Sync a local job to/from the portal (opt-in)
- `adk jobs _exec` — ==SUPPRESS==

## `adk join`

One-command community node onboarding (GitHub auth + hardware detection + serve + mesh join + earnings)

| option | type | required | default | description |
|---|---|---|---|---|
| `--no-github` | str |  | `false` | Skip GitHub auth (use existing token or env) |
| `--cloud-provider` | str |  |  | Cloud provider for remote deployment (deferred to P2) |
| `--model` | str |  |  | Override the resolved inference model |
| `--no-browser` | str |  | `false` | Do not attempt browser open for GitHub auth |
| `--dry-run` | str |  | `false` | Walk the full plan without side effects |

## `adk keys`

Manage cloud provider API keys (set, list, test, remove)

**Subcommands**

- `adk keys set` — Set a provider API key
- `adk keys list` — Show configured provider keys and status
- `adk keys pull` — Sync DOWN from AitherOS: show which providers have keys in your workspace vault
- `adk keys test` — Test API keys (all or specific)
- `adk keys remove` — Remove a provider key

## `adk listen`

Real-time audio intelligence — audiobook, meeting, voice notes

**Subcommands**

- `adk listen audiobook` — Audiobook companion — track characters, stats, spells
- `adk listen meeting` — Meeting transcription — action items, decisions, key points
- `adk listen note` — Voice note — quick dictation with key point extraction
- `adk listen sessions` — List active listening sessions
- `adk listen stop` — Stop a listening session
- `adk listen export` — Export session as markdown notes or transcript

## `adk login`

Authenticate with Aitherium (browser device flow)

| option | type | required | default | description |
|---|---|---|---|---|
| `--email` | str |  |  | Use email/password instead of browser flow |
| `--password` | str |  |  | Password (prompted if --email given without it) |
| `--github` | str |  | `false` | Sign in with your GitHub identity (device flow) |
| `--api-key` | str |  |  | Save an API key directly (no login flow) |
| `--no-sync` | str |  | `false` | Skip auto-syncing your secrets vault after login |
| `--portal-url` | str |  |  | Portal/Identity URL (default: portal.aitherium.com) |

## `adk logout`

Clear saved auth tokens

## `adk mcp`

MCP server, IDE setup, and cloud gateway connection

**Subcommands**

- `adk mcp serve` — Start stdio MCP server (for Claude Code)
- `adk mcp config` — Print MCP client configuration
- `adk mcp setup` — Generate IDE config (.mcp.json) for MCP gateway
- `adk mcp node` — Start lightweight local MCP server
- `adk mcp status` — Check MCP gateway connectivity and tier

## `adk mesh`

AitherMesh overlay operations (onboard, list peers)

**Subcommands**

- `adk mesh onboard` — Onboard this node into AitherMesh overlay (WireGuard)
- `adk mesh ls` — List peer agents in the mesh and their A2A services
- `adk mesh provide` — Become a community inference provider (advertise → consent → await operator trust)
- `adk mesh serve` — Serve Kimi-K3 from this mesh (plan / rpc-backend / coordinator roles)
- `adk mesh leave` — Leave the community inference pool (drain your node's backend from routing)
- `adk mesh federation-token` — Mint your node's relay-federation token (AITHER_NODE_TOKEN for the community hub)
- `adk mesh flux-node` — Start a Flux event-plane listener on this node (participates in AitherMesh)
- `adk mesh create` — Create your OWN isolated mesh (per-tenant overlay CIDR + Headscale key + registry)
- `adk mesh link` — Link your mesh with another (both owners consent -> shared inference pool)

## `adk new`

Scaffold a full template app (e.g. deep-research)

| option | type | required | default | description |
|---|---|---|---|---|
| `<template>` | str | yes |  | Template name, e.g. deep-research |
| `-d`, `--directory` | str |  |  | Target directory (default: ./<template>) |

## `adk notebook`

Plan, run, and inspect Agent Notebooks (.anb) on Genesis

**Subcommands**

- `adk notebook plan` — Create a notebook from a natural-language task
- `adk notebook list` — List Agent Notebooks
- `adk notebook get` — Show a notebook definition (cells, spec, variables)
- `adk notebook run` — Execute a notebook; returns a run handle
- `adk notebook status` — Show a run's status, cell traces, and cost
- `adk notebook export` — Export a notebook to a Jupyter .ipynb file

## `adk onboard`

Interactive onboarding — detect, configure, integrate

| option | type | required | default | description |
|---|---|---|---|---|
| `--api-key` | str |  |  | AITHER_API_KEY |
| `--tenant` | str |  |  | Tenant slug to associate this node with |
| `--agent` | str |  |  | Register a running agent with the portal fleet |
| `--non-interactive` | str |  | `false` | Skip prompts, use defaults |
| `--quick` | str |  | `false` | One-command: auto-run inference, install default pack, enroll |
| `--pack` | str |  | `openclaw` | Pack to install (default: openclaw) |
| `--webgpu` | str |  | `false` | Self-bootstrap onto in-browser WebGPU inference (no server model — the GUI runs the model on the user's GPU) |
| `--discord` | str |  | `false` | Automated onboarding: deploy your agent as a Discord bot (validate token live, print invite link, verify identity/tools, optional --run) |
| `--identity` | str |  |  | Agent identity for the Discord bot (default: $ADK_AGENT or 'aither') |
| `--token` | str |  |  | Discord bot token (or set DISCORD_BOT_TOKEN) |
| `--tools-module` | str |  |  | optional module path whose @tool tools register (e.g. pack.tools.shop) |
| `--run` | str |  | `false` | launch the Discord bot after onboarding (stays connected) |
| `--skip-pack-install` | str |  | `false` | don't `adk install` the --pack (assume it's already installed) |

## `adk pack`

Manage ToolPack extensions (list, search, install, remove, info)

**Subcommands**

- `adk pack list` — List available and installed packs
- `adk pack search` — Search packs by name, description, or tags
- `adk pack install` — Install a tool pack
- `adk pack sync` — Install every entitled pack not already present (license-driven)
- `adk pack buy` — Autonomously buy a pack with Aitherium credits (no Stripe)
- `adk pack negotiate` — Haggle with the seller Broker for a better price
- `adk pack remove` — Remove an installed pack
- `adk pack update` — Update one or all installed packs
- `adk pack export` — Export offline bundle (.tar.gz)
- `adk pack info` — Show pack details
- `adk pack customize` — Customize installed pack (system_prompt, capabilities, domains)
- `adk pack import` — Import an external agent (e.g., Eve) to AitherADK pack

## `adk packs`

List available agent packs

## `adk pair`

Pair this machine with the portal as an inference node (6-char code from the portal)

| option | type | required | default | description |
|---|---|---|---|---|
| `<code>` | str | yes |  | Pairing code shown in the signed-in portal tab |
| `--portal` | str |  |  | Portal base URL (default: https://portal.aitherium.com) |

## `adk platform`

Internal platform toolkit (merged from aither-platform)

| option | type | required | default | description |
|---|---|---|---|---|
| `<platform_args>` | str |  |  | Platform subcommand args |

## `adk publish`

Publish agent to Elysium marketplace

| option | type | required | default | description |
|---|---|---|---|---|
| `<name>` | str |  |  | Agent name (default: from config.yaml) |
| `-d`, `--directory` | str |  |  | Project directory (default: .) |
| `--api-key` | str |  |  | AITHER_API_KEY |
| `--gateway` | str |  |  | Gateway URL (default: gateway.aitherium.com) |
| `--description` | str |  |  | Agent description for marketplace |
| `--capabilities` | str |  |  | Comma-separated capabilities |
| `--version` | str |  |  | Agent version (default: 0.1.0) |
| `--pricing` | str |  | `free` | Pricing model: free, per_request, flat_monthly |
| `--tier` | str |  | `agent` | Agent tier: reflex, agent, reasoning, orchestrator |
| `--category` | str |  | `general` | Category: general, engineering, content, research, security |
| `--dry-run` | str |  | `false` | Validate without publishing |

## `adk publish-preflight`

Check a package can actually be published: an interpreter that meets requires-python, and a wheel that installs AND imports

| option | type | required | default | description |
|---|---|---|---|---|
| `<path>` | str |  | `.` | Package directory (default: the current one) |
| `--import-name` | str |  |  | Module to import, when it legitimately differs from the distribution name |
| `--diagnose` | str |  |  | Translate a publish error into its cause instead of running the checks |

## `adk quickstart`

One-command setup: GPU + auth + shell

| option | type | required | default | description |
|---|---|---|---|---|
| `--api-key` | str |  |  | AITHER_API_KEY |
| `--cloud` | str |  | `false` | Cloud-only setup (no GPU required) |

## `adk quickstart-local`

Local inference quickstart (no cloud required)

| option | type | required | default | description |
|---|---|---|---|---|
| `--backend` | str |  | `auto` | Inference backend (auto = detect best fit) |
| `--model` | str |  |  | Model name/ID (for Ollama; others auto-detected) |
| `--port` | int |  | `8209` | Port for local inference endpoint (default: 8209) |
| `--dry-run` | str |  | `false` | Show what would happen without making changes |
| `--api-key` | str |  |  | AITHER_API_KEY |

## `adk register`

Create a new Aitherium account

| option | type | required | default | description |
|---|---|---|---|---|
| `--email` | str |  |  | Account email (prompted if omitted) |
| `--password` | str |  |  | Account password (prompted if omitted) |

## `adk relay`

Connect this agent to AitherRelay chat (join + serve DMs)

**Subcommands**

- `adk relay join` — Join AitherRelay and answer DMs on this agent's own inference
- `adk relay provision` — Enroll a fleet agent so it may DM humans (binds nick -> your owner identity)
- `adk relay notifications` — Get stored notifications for this agent (one-shot)
- `adk relay up` — Start a sovereign AitherNet relay (Docker compose bundle)

## `adk reregister`

Re-register endpoint(s) with A2A public keys (backfill for existing endpoints)

| option | type | required | default | description |
|---|---|---|---|---|
| `--name` | str |  |  | Re-register one endpoint by name |
| `--all` | str |  | `false` | Re-register all endpoints for this agent |
| `--token` | str |  |  | Portal token (or $AITHER_PORTAL_TOKEN / 'adk login') |
| `--portal` | str |  | `https://veil.aitherium.com` | Portal URL (default: veil.aitherium.com) |

## `adk routing`

Manage per-intent model routing (which model handles which task)

**Subcommands**

- `adk routing preset` — Apply a routing preset (budget, balanced, quality)
- `adk routing set` — Set model for an intent type
- `adk routing reset` — Reset to effort-based routing (disable intent overrides)

## `adk run`

Start the agent server

| option | type | required | default | description |
|---|---|---|---|---|
| `-i`, `--identity` | str |  |  | Agent identity |
| `-p`, `--port` | int |  |  | Server port |
| `--host` | str |  |  | Server host |
| `-b`, `--backend` | str |  |  | LLM backend |
| `-m`, `--model` | str |  |  | Model name |
| `-f`, `--fleet` | str |  |  | Fleet YAML config |
| `-a`, `--agents` | str |  |  | Comma-separated agent identities |
| `--mesh` | str |  | `false` | Enable mesh hosting (advertise tools/inference to connected desktop) |

## `adk sandbox`

Self-host AitherSandbox + link it to your portal (optional safe-testing)

**Subcommands**

- `adk sandbox up` — Deploy the sandbox container, open a tunnel, register with the portal
- `adk sandbox down` — Stop the sandbox container + tunnel
- `adk sandbox status` — Show sandbox state + linked URL

## `adk secret`

Manage secrets (list, get, set, pull, push, sync)

**Subcommands**

- `adk secret list` — List all stored secret keys (values not shown)
- `adk secret get` — Get a secret value
- `adk secret set` — Store a secret in encrypted keyring
- `adk secret pull` — Pull secrets from platform vault
- `adk secret push` — Push a secret to the platform vault
- `adk secret sync` — Bidirectional sync (pull + push local-only)

## `adk setup`

Interactive GPU setup wizard (vLLM/Ollama) + optional AitherOS stack

| option | type | required | default | description |
|---|---|---|---|---|
| `<shortcut>` | str |  |  | Quick setup: 'nemotron' (--tier lite), 'llamacpp' / 'local' / 'endpoint' (native local orchestrator, no Docker) |
| `--mode` | str |  | `auto` | Setup mode: auto (detect GPU), cloud (cloud-only, no GPU), hybrid (local + cloud reasoning) |
| `--tier` | str |  |  | Force a specific tier (default: auto-detect from GPU). 'llamacpp' = native local Nemotron-Orchestrator-8B for endpoints (Windows/macOS/Linux, no Docker) |
| `--backend` | str |  |  | Backend engine override (default inferred from --tier) |
| `--llamacpp-quant` | str |  |  | llama.cpp GGUF quant (e.g. Q4_K_M, Q5_K_M, Q8_0). Default: auto-pick from VRAM/RAM |
| `--llamacpp-port` | int |  |  | llama.cpp server port (default: 8209) |
| `--no-service` | str |  | `false` | llama.cpp: skip installing system service (systemd/launchd/scheduled task) |
| `--reasoning-api` | str |  |  | Cloud API for reasoning (effort 7+) — hybrid mode |
| `--reasoning-model` | str |  |  | Specific model for reasoning backend |
| `--dgx-spark` | str |  |  | DGX Spark / remote vLLM URL (e.g. http://192.168.0.33:8000) |
| `--stack` | str |  |  | Also deploy AitherOS services via AitherZero |
| `--dry-run` | str |  | `false` | Show what would happen without making changes |
| `--non-interactive` | str |  | `false` | No prompts — auto-accept defaults (for CI/automation) |
| `--hf-token` | str |  |  | HuggingFace token for gated models |
| `--api-key` | str |  |  | AITHER_API_KEY for cloud + stack deployment |
| `--output` | str |  | `docker-compose.vllm.yml` | Output compose file path (default: docker-compose.vllm.yml) |
| `--force` | str |  | `false` | Start new containers even if inference is already running |

## `adk setup-all`

Install/set up all AitherOS client products (adk + shell + node + connect)

| option | type | required | default | description |
|---|---|---|---|---|
| `--only` | str |  |  | Comma list — install ONLY these (adk,shell,node,connect,aitherzero) |
| `--skip` | str |  |  | Comma list — skip these products |
| `--with-stack` | str |  |  | Also deploy the AitherZero stack via `adk setup --stack` (e.g. core, full) |
| `--dev` | str |  | `false` | Editable install of awdk from the local checkout (pip -e) |
| `--dry-run` | str |  | `false` | Print the install plan without doing anything |
| `--strict` | str |  | `false` | Abort on the first failed product (default: best-effort, continue) |
| `--yes`, `--non-interactive` | str |  | `false` | Non-interactive |

## `adk shell`

Launch AitherShell interactive terminal

| option | type | required | default | description |
|---|---|---|---|---|
| `--install` | str |  | `false` | Download/update the AitherShell binary |
| `--api-url` | str |  |  | Backend URL (Genesis or ADK server) |
| `--genesis` | str |  |  | Legacy alias for --api-url |
| `<shell_args>` | str |  |  | Arguments to pass to AitherShell |

## `adk skills`

Manage learned skills

**Subcommands**

- `adk skills list` — List all learned skills
- `adk skills search` — Search skills
- `adk skills export` — Export skills in agentskills.io format

## `adk soul`

Import/export SOUL.md identity files

**Subcommands**

- `adk soul import` — Import a SOUL.md file
- `adk soul export` — Export identity as SOUL.md

## `adk ssh`

Open a remote terminal into a prod/dev environment via the tunnel

| option | type | required | default | description |
|---|---|---|---|---|
| `<container>` | str |  |  | Dev-workspace container to attach to (optional) |
| `--container` | str |  |  | Dev-workspace container (alternative to positional) |
| `--tunnel-url` | str |  | `tunnel.aitherium.com` | Tunnel host (default: tunnel.aitherium.com) |
| `-x`, `--exec` | str |  |  | Run CMD headlessly (no TTY) and exit with its code; repeatable |
| `--timeout` | float |  | `120` | Headless run cap in seconds |
| `--json` | flag |  |  | Headless: print `{code, output, reason}` as JSON instead of streaming |
| `-- <cmd…>` | — |  |  | Everything after `--` is one command line to run headlessly |

Headless mode works on Windows and without a TTY (CI, cron, PowerShell, Claude
Code / Codex). Without a container you get the tunnel's restricted allow-listed
shell (`docker`, `curl`, `cat`, `grep`, `ps`, `git`, … — one command per line,
no `;`); with one, the workspace's tmux-backed bash and the real `$?`:

```bash
adk ssh -x "docker ps"
adk ssh -x "docker logs --tail 50 aitheros-arc-solver"
adk ssh devws-you -- make test          # exit code is the remote $?
adk ssh --json -x "docker ps"           # for scripts
```

## `adk ssh-cert`

Fetch a short-lived SSH certificate from the AitherCert SSH CA (GitHub org SSH)

| option | type | required | default | description |
|---|---|---|---|---|
| `--github-user` | str |  |  | GitHub login the cert is bound to |
| `--key` | str |  |  | Public key to certify (default: ~/.ssh/id_ed25519.pub) |
| `--ttl-hours` | int |  | `24` | Certificate lifetime in hours, max 168 (default: 24) |
| `--cert-url` | str |  |  | AitherCert base URL (default: $AITHER_CERT_URL or https://localhost:8113) |

## `adk stack`

Start the consumer stack (Room + Ollama) as native processes

| option | type | required | default | description |
|---|---|---|---|---|
| `<service>` | str |  | `default` | Service to start: default (Room+Ollama), qdrant (local Qdrant) |
| `--interval` | int |  | `10` | Health check interval in seconds (default: 10) |
| `--no-sync` | str |  | `false` | Skip the license-driven pack sync before starting |

## `adk start`

Start chatting with your codebase (zero config)

| option | type | required | default | description |
|---|---|---|---|---|
| `<path>` | str |  | `.` | Project directory (default: current) |
| `--model` | str |  |  | Model: deepseek-flash, deepseek-pro, ollama, openrouter, or a model slug |
| `--provider` | str |  |  | Provider: deepseek, openrouter, ollama, openai, anthropic |
| `--mcp` | str |  | `false` | Connect to AitherOS MCP gateway (1200+ tools) |

## `adk status`

Show backend and service status

| option | type | required | default | description |
|---|---|---|---|---|
| `--json` | str |  | `false` | Machine-readable JSON (agent state) for AI agents/CI |

## `adk support`

Get help — Discord, GitHub, docs

## `adk sync`

Sync local directory with AitherOS platform

**Subcommands**

- `adk sync init` — Initialize sync root
- `adk sync status` — Show sync status (changed/new/deleted)
- `adk sync push` — Upload local changes to platform
- `adk sync pull` — Download remote changes
- `adk sync watch` — Auto-sync on file changes (requires watchdog)
- `adk sync stop` — Stop background watcher
- `adk sync ignore` — Add ignore pattern
- `adk sync config` — Show sync configuration

## `adk test`

Run agent tests

| option | type | required | default | description |
|---|---|---|---|---|
| `-d`, `--directory` | str |  |  | Project directory (default: .) |
| `-v`, `--verbose` | str |  | `false` | Verbose output |
| `--coverage` | str |  | `false` | Show coverage report |

## `adk tools`

Manage available tools (list, sync from platform)

**Subcommands**

- `adk tools list` — List available tools (local + MCP)
- `adk tools sync` — Sync entitled tools from platform

## `adk train`

Manage model training (launch, monitor, cancel)

**Subcommands**

- `adk train status` — Check training readiness and active runs
- `adk train launch` — Launch a training run
- `adk train logs` — Stream training logs for a run
- `adk train cancel` — Cancel an active training run
- `adk train runs` — List recent training runs
- `adk train register-gpu` — Register your local GPU for training

## `adk ui`

Manage the agent's web UI pack (ls / set / path)

**Subcommands**

- `adk ui ls` — List available UI packs (marks the selected one)
- `adk ui set` — Select a UI pack (persists to ~/.aither/config.json)
- `adk ui path` — Show the selected pack + drop-in dir + whether it resolves

## `adk up`

Run a persistent agent connected to your AitherOS fleet (one command)

| option | type | required | default | description |
|---|---|---|---|---|
| `--identity` | str |  | `aither` | Agent identity (default: aither) |
| `--name` | str |  |  | Fleet label for this agent (default: <hostname>-adk) |
| `--provider` | str |  |  | Cloud provider if no local LLM: deepseek/openai/anthropic |
| `--model` | str |  |  | Model name (default: the provider's default) |
| `--port` | int |  | `8080` | Local port for aither-serve (default: 8080) |
| `--yes`, `--non-interactive` | str |  | `false` | Non-interactive: zero prompts, machine-readable JSON (for AI agents/CI) |
| `--foreground` | str |  | `false` | Block the terminal instead of detaching |
| `--no-persist` | str |  | `false` | Do not install a reboot-autostart entry |
| `--force` | str |  | `false` | Restart even if an agent is already running |
| `--offline` | str |  | `false` | Sovereign/local-only: no tunnel, no portal (or set AITHER_OFFLINE=1) |
| `--no-register` | str |  | `false` | Run locally only — no tunnel, no fleet registration |
| `--require-register` | str |  | `false` | Fail (non-zero) if the fleet registration cannot complete |
| `--token` | str |  |  | Portal token for registration (else 'adk login' / $AITHER_PORTAL_TOKEN) |
| `--auth-token` | str |  |  | Callback bearer the control plane presents back (minted if omitted) |
| `--passphrase`, `--pin` | str |  |  | Memorable secret to authenticate remote chat (else a random token is minted). Enter it on the chat page's access gate from your phone. |
| `--email` | str |  |  | Email the phone-ready access link to this address once the tunnel is up (uses configured SMTP; else saved notify_email). |
| `--approve` | str |  |  | Comma-list of tools that pause for approval (default: file_write,shell_exec,shell) |
| `--portal` | str |  | `https://veil.aitherium.com` | Control-plane base URL |
| `--login-url` | str |  |  | Device-flow login base URL |
| `--register-url` | str |  |  | Full fleet-register URL (overrides --portal) |
| `--reach` | str |  | `tunnel` | Connectivity mode: tunnel (Cloudflare, default) or mesh (overlay IP) |
| `--dry-run` | str |  | `false` | Show what would happen without starting anything |

## `adk upgrade`

Open upgrade/checkout page for a pack or plan

| option | type | required | default | description |
|---|---|---|---|---|
| `<target>` | str |  |  | Pack ID or plan: managed, setup, grid, demiurge, pro |

## `adk vault`

Lockbox for the live secrets vault (setup, ls, get, search, rotate, lock)

**Subcommands**

- `adk vault gui` — Open the vault in a browser (starts the console, opens the panel)
- `adk vault setup` — One-time: seal the vault master key into the OS keychain
- `adk vault status` — Show setup + reachability + secret count
- `adk vault ls` — List secret names + metadata (never values)
- `adk vault get` — Reveal one secret (clipboard by default)
- `adk vault search` — Fuzzy-search secret names
- `adk vault scope` — Re-file a secret's scope/owner (overlay — non-destructive)
- `adk vault rotate` — Mint a fresh strong value for a secret and store it
- `adk vault lock` — Drop the PIN session (--forget wipes the sealed key)

## `adk voice`

Voice services (serve standalone HTTP server)

**Subcommands**

- `adk voice serve` — Start the HTTP voice server (default port 8085)

## `adk whoami`

Show current auth status, config and entitlement tier

| option | type | required | default | description |
|---|---|---|---|---|
| `--json` | str |  | `false` | machine-readable output (exit 0 = authenticated) |

## `adk wizard`

First-run wizard — hardware detection, setup recommendations, auth token

| option | type | required | default | description |
|---|---|---|---|---|
| `--yes` | str |  | `false` | Non-interactive mode: accept defaults without prompts |
| `--gui` | str |  | `false` | Launch the point-and-click wizard window (no terminal needed) |

## `adk wm`

World model management (status, inspect, train, reset)

**Subcommands**

- `adk wm status` — List all agents with checkpoints
- `adk wm inspect` — Show learned effects for an agent
- `adk wm train` — Force a bootstrap/refit now
- `adk wm reset` — Delete checkpoint + transitions

## `adk workspace`

Manage dev workspaces on AitherOS tunnel

**Subcommands**

- `adk workspace create` — Create a cloud dev workspace
- `adk workspace bundle` — Download a dev workspace bundle (docker-compose + WireGuard)
- `adk workspace list` — List your active workspaces
- `adk workspace submit` — Submit changes from workspace (commit + PR)
- `adk workspace scopes` — List available scope templates

## `adk x-session`

Bootstrap the autonomous X poster's logged-in session

**Subcommands**

- `adk x-session import` — Verify and store an exported logged-in x.com session
- `adk x-session status` — Is the stored X session logged in right now?

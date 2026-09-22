# Changelog

All notable changes to aither-adk will be documented in this file.

## [Unreleased]

### Fixed

- **A self-hosted Bonsai is now found without `adk backend set`.** A server
  started by aitherium.com/install-bonsai.sh (:8080), `adk bonsai-local` (:8090),
  the llamacpp container (:8092) or the selfhost skill (:8889) was invisible to
  every agent path: `LLMRouter` scanned 8120/8200-8203/8000, `adk status` probed
  8209, `adk up`'s preflight and `adk start`'s detector had their own lists, and
  `adk.local_inference.discover_local_endpoint()` — the one function that would
  have found it — was called by nothing (measured 2026-09-12). One ladder now
  lives in `adk.local_inference.SELFHOST_PORTS` (the same ports aitherium.com's
  local-node probe uses) and is honored by `LLMRouter._try_local_selfhost`
  (before the vLLM scan), `adk status` (new `Configured` and `Local` rows),
  `adk up`, and `adk start`. The port scan requires a model in `/v1/models`;
  a bare `/health` 200 no longer counts.
- `adk up --port 8080` on a Bonsai box reported "Agent healthy on :8080" for a
  child that had died on EADDRINUSE, because llama-server's `/health` satisfied
  the poll. The daemon health check now requires OUR body (`agent` + `version`),
  and `adk up` steps past a port another server owns (explicit `--port` on a
  taken port is an error).
- `adk login` / `adk connect --save` overwrote `inference_url` with the cloud
  URL while leaving `default_backend=vllm`, so a re-login after `adk backend
  set vllm --base-url http://127.0.0.1:8080/v1` made every chat 401. A user-set
  backend keeps its URL; the cloud one lands in `gateway_inference_url`.
- `bonsai-local` and `bonsai` are real backend presets (`adk backend set
  bonsai-local`, `adk run --backend bonsai-local`). The old hint from
  `adk bonsai-local`, `adk --backend bonsai-local`, was not a flag; the command
  now persists itself as the configured backend instead.
- `adk quickstart-local` handled `llamacpp|ollama|vllm` but its own picker
  returns `bonsai` on a CPU-only box with Docker → "unknown backend bonsai".
- `adk backend status` printed `Backend: unknown` after a successful
  `adk backend set` (it read only `setup_backend`).

### Added

- `dgg-research` tool pack (`adk/toolpacks/dgg_research/`): five tools for
  working a documentary record — `actor_resolve` (never writes),
  `actor_references`, `evidence_push`, `mention_verify`, `corpus_null`. A thin
  client over the record's own API on purpose: the resolution policy (which
  role may auto-link a fuzzy match, at what score, with what margin) lives
  server-side, so an owner moves the bar without touching an agent. A refusal
  comes back as a RESULT, not an exception — a 400 there carries the reason and
  the fix, and raising it teaches an agent to retry blindly.
- `adk.toolpacks.dgg_research.onboard`: picks the model tier from the host — a
  declared endpoint wins, else Bonsai-27B on a GPU with >=13 GB usable at 85%
  headroom, else Bonsai-4B-Q1 on any CPU. Never falls back to a hosted API; an
  unreadable GPU reports `unknown`, never `absent`.

- `adk briefs list|show`: the executive-brief delivery plane as a command —
  reads the same host store (~/.aither/briefs) the stop hook writes, so an
  agent can answer "what did the sessions do" without hunting transcripts.

## [3.8.24] - 2026-09-21

### Added

- LM Studio (:1234) joins both local-inference ladders: `adk.enrollment.default_candidates`
  (rung 6, proven by `/v1/models`) and `adk.local_inference.SELFHOST_PORTS` (with Ollama :11434).
  A laptop running only LM Studio no longer enrols as `inference_ready: false`. From the
  Personal-AI-Router intake (PR #8622).

## [3.8.23] - 2026-09-21

### Added - `adk.choose` answers locally through awdecide when no door is named
A stranger who pip-installs awdk had no decision door to call and `adk.choose`
raised `DecideUnavailableError` forever. It now answers in-process through
awdecide's Loop when no door was named (or `AITHER_DECIDE_URL=local`) and the
optional package is importable; `/choose` lands as a harness verb.

## [3.8.22] - 2026-09-20

### Added - bug reports file through awreport's redactor
`adk bug` delegates to awreport so a report never carries a raw secret or path.
EC009 (the ecosystem-surfaces ratchet) reaches ZERO with this pairing.

## [3.8.21] - 2026-09-20

### Fixed - `awdk[docs]` needs docling's `convert-core` extra
docling's PDF backend imports PIL at module top, so `format-pdf` alone
ImportErrored on the first PDF. `convert-core` supplies pillow/numpy/scipy/rtree;
pypdf joins the extra as the light rung `adk/docconvert.py` already falls to.

## [3.8.20] - 2026-09-13

### Added - `adk rc`: a path-scoped harness token and sessions over the link
The harness daemon on 127.0.0.1:8362 already served `/sessions/unified`,
`/sessions/{id}/stream`, `/sessions/{id}/input` and `/decisions/*`; `adk rc`
reaches them through the reverse link with a token scoped to those paths.

## [3.8.19] - 2026-09-13

### Added - an outbound reverse link, so a phone is reachable at all
The tunnel's reverse proxy forwarded to a WireGuard peer and nothing else. A
device that cannot hold a WireGuard peer dials OUT and is reachable through
that link.

## [3.8.18] - 2026-09-13

### Fixed - enrollment probes the real inference ladder
A phone's llama-server on :8099 (or awnode on :8090) enrolled as
`inference_ready=false` because enrollment probed only Ollama :11434 and vLLM
:8120. `adk devices` lists what enrolled; `adk quickstart` = login -> enroll ->
status.

## [3.8.17] - 2026-09-12

### Fixed - a self-hosted Bonsai is found by every agent path
On a box where `install-bonsai.sh` had a working llama-server on :8080,
`adk status` probed :8209 and printed DOWN, and `adk up`'s preflight agreed.
Every path now shares one discovery ladder.

## [3.8.16] - 2026-09-09

### Fixed - develop was BEHIND PyPI (3.8.3 vs 3.8.15), no release for 14 days
`adk-auto-release` fires on a bump to `awdk/pyproject.toml`, and every version
it could reach from 3.8.3 was a DOWNGRADE against the 3.8.15 PyPI already
served, so it cut nothing. Bumped past the registry so the lane can fire.

## [3.8.15] - 2026-09-05

### Fixed - two unregistered bricks were failing ~37 other mirrors' publish gates
PUB001 runs inside EVERY brick's boundary gate. `awsettings` and `awstorage`
were registered public and on PyPI at 0.1.0 but in no boundary scan and no
sync lane, so every other brick's gate reported them.

## [3.8.14] - 2026-09-05

### Fixed - release plane unambiguous (3.8.13 -> 3.8.14)
Bump so the source version is ahead of the last release tag (RP007).

## [3.8.13] - 2026-09-05

### Added - one `aitheros` binary owning awdk, awnode, awgym, awsh, awdesk and awconnect

## [3.8.12] - 2026-09-04

### Fixed - the version/tag mismatch and the advertised-command check closed; the onboarding self-test un-broken
`awdk/pyproject.toml` sat at 3.8.11 while `adk-v3.8.11` was already tagged.

## [3.8.11] - 2026-09-01

### Added - awstorage: scan, classify, catalog, diff, propose, reversible apply
New stdlib-only package `AitherOS/packages/awstorage`; `adk storage <...>`
pass-through and the `awdk[storage]` extra. Delete is a same-volume
QUARANTINE with revert; apply refuses outside roots, stale fingerprints and
unapproved classes; every action is ledgered.

## [3.8.10] - 2026-08-30

### Fixed - all five version carriers synced
The 3.8.7/3.8.8/3.8.9 bumps touched only `pyproject.toml`; `awdk/__init__.py`,
npm, brew and `server.json` sat at 3.8.3-3.8.5, so sync-adk's manifest-parity
step failed on every run since 3.8.7.

## [3.8.9] - 2026-08-29

### Fixed - debt-box-contract DEAD steps closed; vibe/studio origins claimed
Router anchor, kvcache crash and surface-registry enumeration each exited 2
(could-not-judge) on every run.

## [3.8.8] - 2026-08-29

### Fixed - 3.8.7 self-tagged; RP006 build-connect allowlist
The gate-coverage plane converged: the fleet-live batch executes, the host
step is wired, a swept checker restored.

## [3.8.7] - 2026-08-29

### Fixed - version 3.8.6 -> 3.8.7: RP007 was red on develop
`adk-v3.8.6` already existed while the source still said 3.8.6, failing the
release-plane step of every debt run.

## [3.8.6] - 2026-08-28

### Added

- Demo lane: switcher landing page and the `/local` console pack served by
  `aither-serve`; Pages-ready demo packs (API base override, hosted chat,
  public `VEIL_URL`); the AitherOS Desktop live at desktop.aitherium.com.
- Local UI pack: MCP section, direct-answer resilience, think-strip; To Do
  lists and a Desktop OS view ported from the partner WebMock2 design.
- `awask` plain-mode direct LLM completion — no agent loop, no 60-second
  tool-loop timeout.

### Fixed

- Decision-card plane: waiting cards get a TTL and cancel-on-resume; the popup
  survives a large store; console-safety fixes (a self-test can never type into
  the owner's real console; "type it into that terminal" actually presses
  Enter and names the card's own terminal tab).
- `adk.embeddings`: the local-vLLM rung dialed `localhost` — inside a
  container that is the container itself, so every in-container consumer fell
  to the 384-d degraded tail while the embeddings lane answered 200. It now
  dials `aither-vllm-embeddings` in-container, `localhost` on the host.
- `planb_ledger sync` docstring neutralizes the tenant name (ADK003).

> 3.8.0–3.8.5 were release-lane bumps during the 2026-08-26 recovery; their
> changes are folded into this entry.

## [3.7.4] - 2026-08-23

### Fixed

- Re-release: 3.7.3 was tagged by a racing auto-release lane on a tree
  WITHOUT the `session_context` fix.

## [3.7.3] - 2026-08-23

### Added

- Campaign memory — the gobbonet harness keeps the notes, scoped by who knows
  them. This release also cut itself.

## [3.7.2] - 2026-08-23

### Added

- Agents know the time (situation-aware session context; ships with
  `@aitherium/shell-cli` 1.18.1).

## [3.7.1] - 2026-08-23

### Fixed

- The installer that can actually install.

## [3.7.0] - 2026-08-20

### Added

- Opt-in learning-report channel.
- `awrun` tool category — `queue_submit` / `queue_list` / `queue_status` /
  `queue_bump` / `queue_cancel`.
- The rest of the aw family as extras — memory, snapshots, seal, share.

## [3.6.1] - 2026-08-19

### Fixed

- Release repair: the develop merge left the tree unreleasable; 3.6.1 ships it
  clean.

## [3.6.0] - 2026-08-19

### Changed

- Renamed `aither-adk` → `awdk` (the old name keeps resolving via the
  `aither-adk` redirect package). Agents now natively consume the aw family,
  and the aw* bricks integrate as optional capabilities.

> 3.3.0–3.5.0 shipped from the aither-adk mirror lane during the rename window
> and are not documented in this file.

## [3.2.0] - 2026-08-15

### Added — External thinking: a scratchpad for models whose reasoning was taken away

Providers stopped returning raw chain-of-thought. The technique Can Bölük shipped
in Oh My Pi (`externalThinking`, MIT) recovers it without any jailbreak: turn the
model's NATIVE reasoning channel off, then hand it a tool whose only parameter is
a string described as a private scratchpad. The model keeps reasoning — it writes
into the tool call, and tool calls come back in plaintext. What returns is not a
cleaned-up summary; it is the model's own shorthand.

New pack `adk.packs.omp_thinking`:

- `deep_think(thoughts)` — the scratchpad itself.
- `deep_think_supported(...)` — the capability table, ported from upstream's
  `supportsExternalThinking`. It is a REFUSAL list first: a model that cannot
  suppress its native channel is refused, never attempted, because arming it
  there yields two reasoning channels or a rejected request.
- `deep_think_directive(effort)` — with the vendor's thinking channel off, its
  effort dial is inert. The number that still steers the model is the one it can
  read, so this writes it into the system prompt and aims it at the scratchpad.
- `reconcile(registry, model, enabled=True)` — arms or disarms for the CURRENT
  model. **Call it on every model swap.** Whether the scratchpad is legal is a
  property of the model, not the session.

> **Name note:** this `deep_think` is the scratchpad TOOL — somewhere to write
> reasoning. A `deep_think`/`deep_thinking` *flag* meaning "escalate to a more
> expensive search path" is a different thing. Same word, two planes.

### Added — Oh My Pi interop (`adk.packs.omp_interop`)

An omp session recorded with `externalThinking` on already holds raw reasoning in
its `think` tool calls, which makes an omp history a corpus source that cost
nothing to produce. `omp_session_import` reads it, `omp_tool_map` translates omp
tool names to adk equivalents, `omp_locate` finds the databases.

Session stores are opened READ-ONLY, and the schema is DISCOVERED rather than
assumed: an unrecognised layout returns `ok=False, reason="unknown_schema"` with
the tables it found. An importer that returns `[]` there is indistinguishable
from one pointed at a database with no traces in it, and the two call for
opposite responses.

### Added — DeepSeek Coder (`adk.packs.deepseek_coder`)

Two things the chat models cannot do:

- **Fill-in-the-middle.** `dsc_infill(prefix, suffix)` writes the code BETWEEN
  two fragments. Ask a chat model to fill a gap and it rewrites your surrounding
  lines; that is a different operation.
- **Repo-level packing.** `dsc_repo_context` concatenates a project
  dependency-first with `#path` markers — the layout these models were
  pre-trained on. It implements Algorithm 1 of the DeepSeek-Coder paper
  (disconnected subgraphs, then `argmin(in_degree)`, which is what makes the sort
  total on a cyclic import graph). Cycles are reported, never silently broken.
  The dependency graph is returned alongside the packed string.

`dsc_traps` lists the silent failure modes as data, because every way to
misformat a prompt for this family yields a fluent, confident, wrong answer with
nothing logged: the FIM sentinels are U+FF5C and U+2581 (not `|` and `_`), the
suffix goes AFTER the hole marker, and an instruct model needs stop token 32014
to do raw completion or it halts at the first turn boundary.

### Added — `ToolRegistry.unregister(name)`

Registration was one-way. That is fine for a capability that is present or absent
for a whole process, and wrong for a tool whose legality depends on the current
model — the scratchpad stayed armed after a model swap, with no way to remove it.

### Note on 3.0.6 – 3.1.1

Those releases shipped without changelog entries. They are not reconstructed here
rather than guessed at; this entry covers 3.2.0 only.

## [3.0.5] - 2026-08-07

### Fixed — MCP Registry namespace is CASE-SENSITIVE

3.0.4 published to PyPI correctly and the registry publish 403'd:

    You have permission to publish: io.github.Aitherium/*
    Attempting to publish:          io.github.aitherium/aither-adk

The namespace is derived from the GitHub owner VERBATIM, and our org is
`Aitherium`, not `aitherium`. Renamed to `io.github.Aitherium/aither-adk` in
both `server.json` and the README ownership marker — they must match exactly,
case included, because the registry proves ownership by string-matching
`mcp-name: <name>` against the PUBLISHED PyPI description. That is also why this
is a release and not a commit.

Worth noting the workflow behaved correctly: it waited for PyPI, asserted the
marker was in what PyPI actually serves, and only then failed at the publish
itself with the registry's own diagnostic.

## [3.0.4] - 2026-08-07

### Added — listed in the official MCP Registry

`adk mcp serve` has always been a working stdio MCP server (7 tools: file
read/write/edit/list/search, web_search, web_fetch) and was discoverable by
nobody. It is now published to `registry.modelcontextprotocol.io` as
`io.github.aitherium/aither-adk`, which is the index Claude Desktop, Claude
Code, VS Code, Cursor, Windsurf and Goose read.

- `server.json` at the package root, schema-validated against
  `2025-12-11/server.schema.json`.
- An `mcp-name:` marker in this README. That is not decoration: the registry
  proves ownership of a PyPI package by fetching its PUBLISHED description and
  looking for that exact string, which is why this needed a release rather than
  a commit.
- Publishing is a GitHub Actions job on the public mirror using OIDC — no token,
  no secret, and it re-publishes on every version tag instead of drifting until
  someone remembers. It refuses to publish before the version is live on PyPI,
  asserts the marker in what PyPI actually serves, and asserts the entry is
  queryable afterwards, because `publish` exiting 0 does not prove a listing
  exists.

## [3.0.3] - 2026-08-07

### Fixed — the device flow both ACP auth methods depend on was dead

3.0.2 advertised `aither-device` and `aither-terminal` and neither could
complete. FOUR defects stacked, so fixing any one only revealed the next:

- **`DEFAULT_PORTAL_URL` was `https://api.aitheros.ai`, which does not resolve**
  (`getaddrinfo failed`). Every developer box here has `AITHERIDENTITY_URL` set,
  so the dead default only ever reached strangers. Now
  `https://idp.aitherium.com/identity` — the value AitherIdentity's OWN
  discovery document advertises, not a hand-picked host.
- **`POST /oauth/device/code` 404s.** The real path is `/auth/device/code`,
  which this same module's `autonomous_agent_login` had always used; the two
  had silently diverged.
- **A form-encoded body 422s** — the endpoint is a FastAPI model, so it takes
  JSON. Same for the token poll.
- **A pending poll answers HTTP 200 with `{"status": "authorization_pending"}`,
  not RFC 8628's 400 + `error`.** Reading only `error` left it empty on every
  tick, so the loop raised `device login failed: 200` on the FIRST poll. Both
  shapes are now accepted, and a server-suggested `interval` is honoured.

A failure now names the endpoint and the server's reason —
`device login failed: 400` named neither, which is what made this slow to find.
`adk acp login` likewise prints the exception TYPE, because several httpx errors
carry an empty `str()` and it was printing "Could not reach AitherIdentity:"
with nothing after it.

`tests/test_device_login_contract.py` pins all of it, mutation-checked against
each of the four originals — including a DNS assertion on the shipped default,
which is the only one a mocked endpoint can never catch.

## [3.0.2] - 2026-08-07

### Added — ACP Registry admission (agentclientprotocol/registry)

- **`initialize` advertises real `authMethods`.** Two AitherIdentity device-flow
  (RFC 8628) methods: `aither-device` (type `agent` — we open the browser and
  poll) and `aither-terminal` (type `terminal` — the client relaunches us as
  `adk acp login`). The registry lists no agent that returns none, and it accepts
  only these two types: a method typed anything else is DROPPED by its verifier,
  so the advertisement looks populated locally and reads as "no auth" in CI.
- **`authenticate` and `logout` do the real thing.** They were stubs returning
  `{}`. An authenticate that returns `{}` without a token tells the client to
  proceed and then fails at the first prompt with an unrelated error; every
  failure path now raises. `logout` clears the `portal` profile and is advertised
  via `agentCapabilities.auth.logout`.
- **`adk acp login`** — interactive sign-in, the Terminal Auth entrypoint. A
  method naming a command that does not exist fails inside the editor, where
  nobody sees the output.
- **`aither-adk` console script.** `uvx <package>` runs the script whose name
  MATCHES the package; without this alias the registry's uvx distribution
  installs and cannot launch, while validating fine (the registry only checks
  that the PyPI package exists, never that it runs).

### Fixed

- **`adk acp serve` no longer probes for an LLM backend at startup.** It awaited
  `get_provider()` before serving, so on a machine with no fleet, no Ollama and
  no API key — exactly the registry's CI runner — the process exited before
  answering `initialize`. That surfaces as "timeout waiting for initialize",
  i.e. as a protocol bug. Loudness moved to the first `session/prompt`, where a
  missing backend is a real turn error.

## [3.0.1] - 2026-08-06

### Fixed — AitherShell (adk-shell) front door and status surfaces

- **`aither "question"` / `aither --print "query"` work.** The first two usage
  lines in the CLI's own help died with `No such command` (a click Group resolved
  the bare query as a subcommand name). An unknown first token now runs as a query.
- **`--status`, `--config`, `--plugins`, `--history` print their output** — all
  four computed their result and silently discarded it.
- **`config show` unshadowed** — the `config` command method was hidden by the
  instance's `config` attribute and failed with "config is not a command".
- **Host-correct genesis default** — `http://127.0.0.1:8001` (the LB's plain-HTTP
  listener) instead of `https://localhost:8001`, which reported UNREACHABLE
  against a healthy genesis.

## [3.0.0] - 2026-08-06

### Added — Agent Client Protocol v2, full two-way

aither-adk now speaks ACP v2 in both directions — as the **agent** an editor
drives, and as the **driver** of any external ACP agent.

- **ACP v2 server** (`adk acp serve`): full prompt lifecycle (user_message
  confirm → running → chunk-streamed agent_message/thought/tool_call updates →
  terminal `state_update: idle` + stopReason), session new/list/resume/close/delete,
  outbound `session/request_permission` mapped onto AitherAgent's human-in-the-loop
  gate, cancel semantics, batch-safe framing. Works with any object exposing the adk
  agent contract, not just AitherAgent.
- **ACP v2 client** (`adk acp`): streaming `stream_prompt`, aggregate `prompt`
  (waits for the terminal idle — text-only turns no longer drop their reply),
  session lifecycle, `session/request_permission` answering, `additionalDirectories`.
- **ACP LLM backend** (`adk backend add acp --command <cmd>`): wrap an external
  ACP agent (claude-agent-acp, codex-acp, gemini-cli, …) as a model/provider for
  AitherAgent — memory and faculties on top of the external agent's loop.
- **CLI**: `adk acp serve / connect / prompt / agents / list-sessions / config <ide>`
- **Tool pack + skills**: `acp_*` fleet tools and `acp-drive` / `acp-serve` skills;
  Supervisor drives `protocol: acp` manifests.

### Fixed — routing honours the configured AitherOS spine

- `LLMRouter` auto-detect could pick up a stray local vLLM (a swap container on
  :8201) BEFORE the configured desktop MicroScheduler — every chat silently went to
  the wrong model. The configured spine (`inference_url` in `~/.aither/config.json`,
  or `AITHER_CORE_LLM_URL`) is now tried FIRST, with the AitherNet internal CA
  trusted via `tls_verify()`.
- The desktop spine is the default provider when configured; a local fast path is
  only reached on an explicit low effort.
- Ollama auto-detect validates against installed models instead of defaulting to a
  phantom `gemma4:4b`.

## [2.48.0] - 2026-08-02

### Added — AitherShell harness layer

- AitherShell as a shell-of-shells over the adk harness; per-session model routing
  via `--setting-sources project,local`.
- Two release gates (ignored-source, doc-drift).

## [2.47.0] - 2026-08-02

### Added — Bonsai as a first-class default backend for low-resource hardware

- **`pick_backend()` now auto-detects and recommends Bonsai** for CPU-only and
  <6GB VRAM hardware. If the Bonsai container is already running or the GGUF is
  downloaded, it is selected automatically — no explicit `--backend bonsai-local`
  needed. This means `adk quickstart` on a phone, a Pi, or a laptop without a GPU
  now defaults to local inference instead of requiring cloud fallback.
- **New `bonsai` hardware profile** (`profiles/bonsai.yaml`) — targets 0 GPU,
  4GB RAM minimum. Documents platform-specific binaries and expected throughput
  for Android (Termux), iOS, Raspberry Pi, Windows, macOS, and Linux.
- **`bonsai-4b` variant** for ultra-constrained devices (2GB RAM, Android phones).
- **README hardware profiles table updated** — Bonsai leads the table as the
  lowest-resource option. `adk setup --tier bonsai` documented.

## [2.46.0] - 2026-07-31

## [2.45.0] - 2026-07-29

### Fixed — streaming against an AitherOS-typed SSE backend returned a silent EMPTY answer

- **`OpenAIProvider.chat_stream` now parses BOTH stream dialects.** MicroScheduler's
  `/v1/chat/completions` facade (and Genesis) answer `stream=true` with AitherOS-typed
  SSE events (`event: token`, `data: {"t": "..."}`) rather than OpenAI chunks
  (`choices[].delta.content`). Parsing only the OpenAI shape yielded **zero chunks and
  no error** — the agent loop (`stream_react`) completed "successfully" with an empty
  answer while non-stream `chat()` worked against the same endpoint, which made every
  `/chat/stream` turn on an MS-backed daemon silently empty. The provider now accepts
  typed `token`/`answer`/`complete`/`error` events alongside OpenAI chunks; a trailing
  `answer` event never duplicates already-streamed tokens. Two regression tests pin
  both dialect paths (`tests/test_llm_providers.py`).

## [2.44.0] - 2026-07-27

### Fixed — `adk enroll` silently registered devices into the WRONG TENANT

- **`adk enroll` succeeded without an identity and bound the device to the tenant
  `"personal"` instead of the caller's workspace.** `_extract_tenant_slug()`
  (`fleet_enroll.py:217`) falls back to `"personal"` when `~/.aither/auth.json` is
  absent, so enrolling a fresh machine reported success while registering it
  somewhere the owner cannot see. Nothing failed, nothing warned, and the endpoint
  simply never appeared in their workspace — the fail-open shape where the
  operation reports success and does the wrong thing.

  This bites hardest on unattended installs (phones, headless VMs, cloud images),
  which are exactly the machines nobody is watching closely enough to notice.

  `cmd_enroll` now **refuses** when no identity is present, and names the two ways
  to supply one:

      adk login                     # browser flow
      adk login --api-key <key>     # headless / phone / VM

  Verified by running it with an empty `HOME`: refuses, **exit 1**. (`AITHER_NODE_TOKEN`
  is accepted as an identity, so token-based fleet enrolment is unaffected.)

  Found while building a one-command self-host installer for Android's Linux
  terminal, by running the installer rather than reasoning about it.

## [2.39.0] - 2026-07-25

### Fixed — containerized agents were unreachable

- **`docker` runtime: added `-i` to `docker run`.** Without it Docker does not
  attach the container's stdin, so *every* containerized stdio agent (acp, mcp)
  silently never received a request. Measured against a real container: no `-i` →
  the child read nothing; with `-i` → the request round-trips. A real customer
  pack *is* a container, so this runtime was effectively unusable.
- **`runtime.cmd` is now `shlex`-split** for the `docker` and `node` runtimes.
  `cmd: "python -u server.py"` was passed as a single argv element, so docker
  tried to exec a binary literally named `python -u server.py`.
- `Supervisor._build_command()` extracted from `spawn()` so argv is testable
  without launching a process — the missing `-i` survived precisely because it
  wasn't.
- Env is deliberately **not** forwarded as `-e KEY=value`: that puts credentials
  in argv, readable by any local `ps` / `docker inspect`.

### Fixed — CodeActLoop `cell_timeout` did not bound CPU-bound cells

- `asyncio.wait_for` cannot preempt a coroutine that never yields, so
  `while True: pass` was never interrupted and hung the agent indefinitely. Now
  also bounded by a line-level trace deadline (`CellTimeout`), scoped to the
  cell's own frames so a cell that awaits cannot fire the deadline inside
  unrelated coroutines.
- The `CodeActLoop` docstring now states plainly that **it is not a sandbox**, and
  names what a cell can actually do (`import os`, `subprocess`, `open(..., "w")`).
  Do not run cells from an untrusted source.

### Added — real containment for CodeAct cells

- **`CodeActLoop(executor=DockerCellExecutor())`** runs each cell in a container
  with `--network none`, a read-only root filesystem plus a noexec/nosuid tmpfs,
  `--cap-drop ALL`, `no-new-privileges`, memory and pid caps, a non-root user, and
  `python -I`. The escapes that succeed in-process — writing a host file, network
  egress, running as root — all fail there, and an infinite loop's container is
  **killed** rather than merely interrupted.
- `return_result()` crosses the process boundary via a stdout sentinel and is
  deliberately **not** `eval`'d back into the host.
- Trade-off stated plainly: an isolated cell is a fresh process, so there is no
  live host namespace and no cross-cell state. Containment *and* a live namespace
  together need fork-from-warm snapshotting (`adk/forkd_client.py`'s Firecracker
  target), which requires Linux ≥ 5.7 + KVM.
- The in-process default is unchanged. Do not point CodeAct at untrusted input
  without `executor=`.

### Fixed — TLS and test-suite integrity

- **`adk/cli.py` sent an `Authorization: Bearer` API key over TLS with
  verification disabled**, exposing it to a machine-in-the-middle. Now routed
  through `adk._tls.tls_verify()`.
- The `verify=False` ratchet regex-scanned raw lines, so docstrings that merely
  *named* the antipattern were reported as violations — noise that hid the real
  hole above. It now walks the AST for actual keyword arguments.
- Five `test_inference_proxy` failures were stale assertions pinning the
  pre-MicroScheduler routing and a 5-model roster (it is 6 since `aither-bonsai`).
  Assertions now derive from the roster and encode the tier-access rule, so a
  future model addition doesn't break them.

### Fixed — MCP stdio hung silently on Windows

- `adk/mcp_stdio.py` and `adk/shell/mcp_bridge.py` attached stdin with
  `loop.connect_read_pipe`, which fails inside the Proactor loop's read handler
  such that the read future never resolves — the server didn't crash, it just
  never answered. Both now use the new shared `adk.stdio_compat`
  (`ThreadStdinReader` / `ThreadStdoutWriter`), which `acp_server.py` also uses.

### Added — per-install pack credentials are now actually used

- `adk/pack_credentials.py` was orphaned: mint/revoke were never called, so pack
  installs kept using the shared credential the module was written to replace.
  Both install paths now mint, and a reinstall revokes the outgoing install's
  credential before its metadata becomes unreachable.
- **New `/packs uninstall <pack-id>`** (aliases `remove`, `rm`) — revokes the
  per-install credential first, then removes the pack. Previously the only revoke
  path was a reinstall, so deleting a pack any other way left a live,
  unrevocable credential. The target is confined to the pack root.

### Fixed — packaging

- The brew formula's own `sha256` was left as `PLACEHOLDER_SHA256` on every
  release, so `brew install` could never verify the download.
  `sync_versions.py` now fills it from PyPI and `--check` **fails** when a
  published version still carries a placeholder or a stale digest.
- `sync_brew` rewrote *every* `url` in the formula, repointing the `httpx`,
  `pyyaml`, `fastapi` and `uvicorn` `resource` blocks at the aither-adk tarball.
  Fixed with `count=1`; the resource digests are now real.
- `adk/__init__.py`'s source-checkout fallback `__version__` had been stale since
  2.32.0 because the canonical release path never ran `sync_versions.py`. That
  check now runs in `sync-adk.yml`.

## [2.38.1] - 2026-07-24

Release plumbing only — 2.38.0 was tagged before the version manifests
(npm/brew/winget) were synced, so its publish gate failed and it never reached
PyPI. 2.38.1 is the same code with the manifests in sync. See 2.38.0 below for
what actually changed.

## [2.38.0] - 2026-07-24 (never published — superseded by 2.38.1)

### Fixed — `adk join` could only ever have worked for us

- **`adk join` defaulted identity to `https://localhost:8115`** — a fleet-INTERNAL
  address. `adk join` is the one command a stranger runs on their own GPU box, so on
  any machine not already running AitherOS locally, step 1 (the GitHub device flow)
  hit a refused connection, and so did node registration and the mesh-key issue. The
  conductor default was already public, which is exactly why this went unnoticed.
  Identity now defaults to `https://idp.aitherium.com`.

  This fix landed *after* the 2.37.0 tag, so **2.37.0 and every earlier release on
  PyPI still ship the broken default** — 2.38.0 is the first release a new
  contributor can actually run. Verified live from a non-fleet machine (DGX):
  `POST /auth/github/device/start` → HTTP 200 with a real `user_code` and
  `verification_uri`.

- **Upgrade check compared versions as strings** — `latest != current` reported a
  downgrade as an available upgrade. Now uses `packaging.version` with a numeric-tuple
  fallback.

## [2.37.0] - 2026-07-24

### Added — Agent Onboarding Fabric: bring your own agent, or run a managed one

Onboard an agent built on **any** framework, or let Aitherium run it for you.

- **ACP (Agent Client Protocol), both directions** — `ACPClient` drives any ACP agent;
  `ACPServer`/`serve_stdio` exposes an adk agent to any ACP host (Zed, VS Code,
  JetBrains). Both proven live against Zed's reference `agent-client-protocol`.
- **Universal Agent Pack** — `AgentPackManifest` + `Supervisor` describe and run an
  external agent across 6 frameworks × 6 protocols, fail-closed on anything unknown.
- **Protocol drivers** — `get_driver()` returns an `http`, `langgraph_rest`, `a2a`, or
  `mcp` driver, so REST/graph-orchestrated agents can be *managed*, not just connected.
- **Pack registry** — `PackRegistry.publish/browse/versions/get/yank/verify_digest`,
  SHA256-digested, semver-ordered, validation fail-closed before anything is written.
- **Managed identity** — `ManagedAgentIdentityProvider` with a real gateway minter and a
  PROVISIONED→REGISTERED→ACTIVE→ROTATED→REVOKED lifecycle; default-deny `authorize()`.
- **Zero-code connect templates** — `render_connect(framework, …)` emits config pinned to
  each framework's real schema.

### Added — NOOA-style agent capabilities

- **`CodeActLoop`** — code as action: the model writes Python cells that run against a
  persistent namespace over live objects, each cell AST-validated before execution, with
  per-cell timeouts and bounded observations.
- **`ObjectRegistry` / `render_observation`** — pass by reference: large values become
  handles instead of flooding the context with serialized text.
- **`EventLog` / `ContextBlocks`** — harness APIs the model itself can call to query its
  own event history and manage named context blocks in KV-cache-stable order.
- **`has_ellipsis_body` / `@strategy`** — NOOA's `async def f(...) -> X: ...` ergonomic.
- **`CodeValidator`** — best-effort AST validation (ported from NVIDIA's OO-Agents,
  Apache-2.0; NOTICE included). A defense-in-depth layer, not a sandbox boundary.
- **`PredictLoop`** — single-shot strategy with structured output (`output_model=`) and
  validation retry; `Strategy` is now a first-class alias of `AgentLoop`, and both loops
  accept a per-call `model=` override.
- **Typed I/O** — `AgentResult` validates its fields at construction and at the
  `Agent.run` boundary.

All new capabilities are exported lazily from the top-level `adk` namespace.

## [2.27.0] - 2026-07-18

### Added — Moonshot Kimi K3 provider (DeepSeek-equivalent cloud backend)

- **New `moonshot` provider** across the LLM router: OpenAI-compatible
  `https://api.moonshot.ai/v1`, default model `kimi-k3` (131K context default, up to 1M;
  tool calls, JSON mode, structured output). Wired everywhere `deepseek` is:
  - `LLMRouter(provider="moonshot")` + `_COMPAT_URLS`/`_COMPAT_MODELS`/model tiers
    (small/medium/large → `kimi-k3`)
  - Config: `moonshot_api_key` field (env `MOONSHOT_API_KEY`), saved-config load,
    provider-keys map (`provider_keys.json` key `moonshot`)
  - Auto-detect chain: tried after DeepSeek (config key, then env key)
  - Hybrid reasoning backend: `AITHER_REASONING_BACKEND=moonshot` for effort 7+
  - Reasoning tiers: `backend: moonshot` (alias `kimi`) via new `MoonshotBackend`
  - CLI: `adk backend set moonshot --api-key …`, `adk backend list` shows moonshot
- No breaking changes; DeepSeek and all other providers unchanged.

## [2.26.0] - 2026-07-16

### Added — Aeon group-chat web UI pack + POST /aeon/stream SSE endpoint

- **New `aeon` UI pack** (`adk/webui/packs/aeon/index.html`) — group chat for multi-agent
  discussion. Mirrors the `llamacpp` pack's auth + SSE pattern (bearer from URL fragment #k=),
  with a preset selector (balanced/creative/technical/security/minimal/duo_code/research) and
  distinct colored agent messages. Synthesis block highlighted. Agent content is HTML-escaped
  before render. Fully self-contained HTML+CSS+JS, light+dark, no external assets. (No
  temperature/max-tokens controls — Aeon runs a fixed multi-agent round and doesn't thread
  per-turn generation params, so exposing them would be a no-op.)
- **POST /aeon/stream SSE endpoint** — accepts body {message, preset?, agents?, rounds?,
  session_id?}, emits SSE with: `session_start` (participants + orchestrator), `agent_message`
  per non-orchestrator, `synthesis` (if enabled), `error` on exception, `complete` (total tokens
  + latency). Mirrors /chat/stream structure — like every streaming endpoint here it is gated by
  the server auth middleware **when `AITHER_SERVER_API_KEY` is set** (a local `adk up` with no key
  is unauthenticated by design, same as /chat/stream).
- **Default UI pack flipped to `llamacpp`** — `resolve_ui_pack_name()` now returns `llamacpp`
  instead of `console` when no env/config is set (new users see the clean chat, not the admin
  SPA; existing users with AITHER_AGENT_UI or saved config unaffected).

### Added — swappable agent web UI packs (llama.cpp-style built-in chat)

- The page an agent serves at `/` is now a **swappable UI pack**, so you can
  drop in, test, and deploy different chat frontends without touching the agent.
  Select with `adk ui set <name>` (persists to `~/.aither/config.json`) or the
  `$AITHER_AGENT_UI` env; `adk ui ls` lists packs, `adk ui path` shows what
  resolves.
- **New built-in `llamacpp` pack** — a clean, self-contained llama.cpp-style
  chat UI (centered conversation, streaming token-by-token with a cursor, a
  Stop button, a settings drawer for temperature / max-tokens that is actually
  honored end-to-end — `/chat/stream` now forwards them to the provider —
  minimal safe markdown, light+dark). Hits the agent's own gated `/chat/stream`
  with the bearer read from the URL fragment (never sent in the URL). Built-ins
  also include `console` (the full admin SPA, still the default) and `minimal`.
- **Drop-in custom packs**: a UI pack is just a folder with an `index.html` in
  `~/.aither/ui-packs/<name>/` (override via `$AITHER_UI_PACKS_DIR`) — no rebuild.
- Resolution is **fail-soft**: an unknown/missing pack falls back
  console → minimal, so `/` is never blank.

### Added — MCP-gateway-first platform access + mesh A2A trust

- **Gateway-first platform access** (`adk/client/_gateway_mcp.py`, `GatewayMCPClient`):
  a self-hosted agent connects its MCP **client** to `mcp.aitherium.com` authenticated
  as the OWNER (device-flow / PAT / ACTA token), RBAC-scoped to that identity — so it can
  use platform tools + `get_secret` **whether or not the node runs local AitherOS
  services**. Fail-closed: no token → client disabled, the agent still runs. Wired into
  the server lifespan (`_connect_gateway_mcp`).
- **`genesis` LLM backend**: `adk backend set genesis` points the agent brain at a local
  Genesis (`http://localhost:8001/v1`, model `workflow`) — the 5090/controller pattern
  where inference is served by the local fleet, not the cloud gateway or a raw vLLM.
- **`adk mesh` command**: `adk mesh onboard` drives the Conductor 5-step onboard
  (`/v1/mesh/onboard` → registers this node's public key, returns the `endpoint:mesh`
  trust token); `adk mesh ls` lists peer nodes/agents (AitherMesh `/nodes` + Directory)
  with their A2A cards + invoke URLs for discovery.

### Security — A2A inter-agent trust enforcement (fail-closed)

- **`adk/a2a_trust.py`** (new): Ed25519 signature verification + trusted-key check
  (`AITHER_A2A_TRUSTED_KEYS`), opt-in via `AITHER_A2A_REQUIRE_TRUST` (default `false` for
  backward compatibility; `audit` logs untrusted, `true` rejects).
- **Fixed a fail-OPEN in the inbound A2A handler** (`adk/a2a.py`): the trust check
  previously ran *only when signature headers were present*, so an unsigned request
  bypassed `AITHER_A2A_REQUIRE_TRUST` entirely. Now genuinely fail-closed — required mode
  with no/invalid signature → `403`.
- **Sovereign template no longer scaffolds `verify=False`**: the generated
  `services/llm.py` now uses the project's own `tls_verify()` policy helper for every
  provider call instead of hardcoding disabled TLS verification.
- **`adk/_tls.py`**: disabling verification via `AITHER_TLS_VERIFY=false` now emits a
  one-time loud warning (was silent) — the escape hatch stays, the silence goes.

## [2.25.1] - 2026-07-15

### Fixed — self-hosted OpenAI-compatible backends honor a custom base URL

- `adk backend set llamacpp --base-url http://localhost:8090/v1 --model <id>` (and
  `vllm`/`lmstudio`, and the `AITHER_LLM_BASE_URL` env) now actually point the agent
  at YOUR server. Previously the base URL was saved but never read, so an explicit
  `--backend llamacpp` fell back to the provider's public API default (openai.com).
  New `Config.llm_base_url` field + saved-config `inference_url` wiring; the explicit
  backend path in `LLMRouter` now passes it for the local OpenAI-compatible family.
  This is what lets a self-hosted node run its OWN local model (e.g. a PrismML
  Bonsai llama.cpp) as the agent brain instead of ollama/cloud.

## [2.25.0] - 2026-07-15

### Added — node_bootstrap tool pack: bootstrap LLM inference on any hardware

- **New bundled tool pack `node-bootstrap`** (`adk/toolpacks/node_bootstrap/`):
  seven agent tools — `node_detect_hardware`, `node_resolve_recipe`,
  `node_plan_deployment`, `node_apply`, `node_enroll`, `node_register_backend`,
  `node_verify` — plus a CLI shim (`python -m adk.toolpacks.node_bootstrap
  detect|resolve|plan|apply|enroll|register|verify`). Detect a box's hardware,
  resolve a deployment recipe, actually deploy the inference engine (docker
  compose / native / delegate), enroll with a control plane, register the
  backend, and live-verify a completion.
- **9 hardware recipes** shipped in the wheel: `cpu-1bit-llamacpp`, `cpu-ollama`,
  `cuda-vllm-8gb/24gb/40gb`, `cuda-dual-stack-32gb` (32GB-card co-resident
  stack), `unified-memory-vllm` (DGX/Grace-class), `metal-ollama` (native —
  Docker on macOS has no Metal passthrough), `cloud-api` fallback. Resolution
  scores hardware against min/max VRAM bands, RAM, cores and unified memory;
  ties prefer higher tiers and self-contained recipes over fleet delegates.
- Security posture: enrollment and backend registration are fail-closed
  (missing token/URL → error dict, never anonymous, never `verify=False`);
  tokens are redacted in outputs; all endpoints come from args/env
  (`AITHER_CONTROL_PLANE_URL`, `AITHER_GENESIS_URL`, `AITHER_AUTH_TOKEN`).

## [2.24.0] - 2026-07-14

### Changed — sync-family consolidation

- **Removed dead code:** `adk/sync.py` (528 lines) deleted — it was unreachable,
  shadowed by the `adk/sync/` package (`import adk.sync` always resolved to the
  package; nothing loaded the module by path — verified).
- **Grouped the sync domain modules into the `adk.sync` package:**
  `brain_sync`→`adk.sync.brain`, `files_sync`→`adk.sync.files`,
  `lockbox_sync`→`adk.sync.lockbox`, `secrets_sync`→`adk.sync.secrets`,
  `session_sync`→`adk.sync.sessions`, `settings_sync`→`adk.sync.settings`,
  `sync_watermark`→`adk.sync.watermark`. All in-tree importers updated. No
  back-compat shims were left because **zero external/monorepo code imported the
  old root paths** (verified) — a clean move, not a shimmed one. **Breaking only
  for code that imported `adk.<name>_sync` directly** (none known).

### Docs — MCP module layering clarified

- `adk.mcp` (enterprise AitherOS gateway client: auth/billing/token-tracking) and
  `adk.core.mcp` (minimal generic JSON-RPC adapter for any MCP server) are
  DISTINCT layers, not duplicates — each now carries a LAYER/ROLE/See-also
  docstring so they are not mistakenly consolidated.

## [2.23.2] - 2026-07-14

### Fixed — CompletionGate no longer false-fails on correct results

- **Tolerant judge JSON extraction.** The LLM judge parsed its reply with `json.loads(text[first"{":last"}"])`, which raised `JSONDecodeError` ("Extra data") whenever the model appended prose after the verdict or emitted a second object — and `verify()` turned that into a fail-closed verdict, so a genuinely completed task reported `unverified`. New `_first_json_object()` uses `JSONDecoder.raw_decode` from the first `{` that begins a valid object and ignores trailing content (prose, markdown fences, extra objects). A judge that answers with no parseable JSON still fails closed (never a soft pass), now with a clear reason. The same extractor backs `_derive_criteria`.
- **Bare code-like tokens are hard-checked.** A natural criterion like `output contains ADK_AUTO_OK` (no quotes) previously fell through to the judge; `hard_checks` now also matches bare identifier/code shapes (contains an underscore, or 4+ CAPS/digit chars), with a stopword guard so a descriptive uppercase word (`OUTPUT`, `CONTAIN`) never becomes a false required token. Quote a token to force it regardless.
- +9 tests (24 total), including the exact trailing-data regression.

## [2.23.0] - 2026-07-14

### Removed — dead modules and internal integrations

- **`adk.swarm`, `adk.provisioning_tools`, `adk.session_sync_integration`, `adk.addon_metering`** — four orphan modules with zero public usage have been deleted. These were internal prototypes kept for legacy monorepo consumers; public packages have not imported them since 2.21.0. **Breaking for anyone directly importing these modules** (import will fail). They are no longer exempt from the orphan-module check.
- **`adk.aither_bridge`** — the AitherOS-internal IRC ↔ chat gateway bridge is no longer shipped in the public aither-adk package (stripped at sync time). All public importers already degrade behind `except ImportError` (in `adk.server` and `adk.builtin_tools`), so public packages remain unaffected. The module stays in the private AitherOS monorepo for internal use.
- **Identity document trim** — removed internal agent names from `adk/identities/aither.yaml` delegation guidance, replacing with role-generic wording ("delegate code review, refactoring, security analysis, performance and testing to specialist agents when available"). Effort-tier routing philosophy remains unchanged and is product design, not a leak.

### Changed — genericized customer/product names in help text

- CLI help/examples no longer name specific customer deployments (`--scope` template list and `deploy agent` name example use generic placeholders). Functional deploy paths are unchanged.

## [2.22.1] - 2026-07-14

### Fixed — real bugs surfaced by reviving the public CI (red since Jul 5)

- **TLS**: bundled toolpacks (arc-brainpack, cloudflare) disabled certificate
  verification on gateway/vault calls; all now route `adk._tls.tls_verify()`
  (verified by default, internal CA bundle when present).
- **`system_prompt` contract**: bundled-pack `[PACK DIRECTIVES]` were appended
  even to an explicit `system_prompt`; an explicit prompt now wins wholesale.
- **Headless secrets**: `_derive_key()` used `os.getlogin()`, which raises
  `OSError` without a controlling terminal (CI, daemons, containers) — falls
  back to `getpass.getuser()` (same value on interactive boxes, so existing
  secret files keep decrypting).
- **`pack_verifier`**: missing `cryptography` crashed with `UnboundLocalError`
  in the exception handler instead of failing closed; now degrades gracefully
  (and `cryptography` was added to the dev extra so CI tests the real path).
- **`fleet_manager._kill_pid` (POSIX)**: reported success immediately after an
  asynchronous SIGTERM and never reaped children (zombies read as alive); now
  polls, escalates to SIGKILL, and reaps.
- **Identity**: `aither` regained the `creative` tool category (dropped
  accidentally in a 2.7.0 WIP sweep).

## [2.22.0] - 2026-07-14

### Changed — internal toolkit relocation

- **`adk.platform` relocated** — the internal aither-platform toolkit (ComfyUI/image-gen
  pipelines, fleet agent clients, parallel CLI/UI/tools stack) has been moved out of the
  adk SDK tree to the standalone internal `aither-platform` package at
  `AitherOS/agents/aither_platform/`. The public package remains unaffected (it already
  excluded `adk.platform` since 2.21.1). Internal AitherOS agent builds now install
  `aither-platform` separately. `adk platform` falls through to the same "not available"
  message on public installs; internal builds work via the relocated package.

### Removed — backward-compat re-export deleted

- **`adk.gateway` module removed** — the backward-compatibility re-export of `GatewayClient`
  from `adk.gateway` has been removed. Use `from adk.client import GatewayClient` instead
  (the established public API since 2.21.0). All internal importers have been updated.

## [2.21.1] - 2026-07-14

### Removed — internal toolkit and license-gated product no longer ship publicly

- **`adk.platform`** — the internal aither-platform toolkit (ComfyUI/image-gen
  pipelines, fleet agent clients, a parallel cli/ui/tools stack) is no longer
  included in the public repo or the PyPI wheel. It was internal-only code that
  had been riding along since the aither-platform merge. `adk platform` now
  reports "Platform toolkit not available" on public installs; internal AitherOS
  builds are unaffected.
- **`adk.formbridge`** — the FormBridge form-automation product is license-gated
  (PROFESSIONAL+); its implementation no longer ships in the free package. All
  import sites (`builtin_tools`, `server` route mount) already degrade behind
  `except ImportError`, so nothing else changes behavior.
- Product test suites (`test_formbridge.py`, `test_encounter_keying.py`,
  `test_flow_tools.py`) are excluded from the public sync alongside them.

## [2.21.0] - 2026-07-14

### Added — Completion gate: verified-or-retried task execution

- **`adk.gate.CompletionGate`** — a composable wrapper that closes the "silent
  success" gap: an agent's ReAct loop returns whatever the model produced and
  calls it done (`finish_reason = stop/max_steps/length`), which is a *liveness*
  signal, not a *completion* one — so an agent can "succeed" while doing nothing.
  `CompletionGate` runs the agent, verifies the result against acceptance
  criteria, retries with the failure fed back, and returns the response with
  `finish_reason` set to `verified` or an honest `unverified`. Never a false pass.
  - **Hard-first, un-soft-passable checks** (`adk.gate.hard_checks`): a filesystem
    path a criterion says must exist is checked on disk; a required quoted token
    must appear in the output. Only genuinely subjective criteria fall through to
    an optional LLM judge (reuses the agent's own model), which **fails closed**
    if unavailable.
  - **`gated_run(agent, task, criteria=..., max_retries=2)`** — one-shot helper.
  - `auto_criteria` (LLM writes the machine-checkable definition-of-done from the
    task), a `verifier=` override for custom checks (tests-pass / HTTP probe), and
    a `requires_action` short-circuit so it never fights human-approval pauses.
  - Compose it onto any agent exposing `run(task) -> resp{.content, .finish_reason}`
    (`AitherAgent`, `core.Agent`, or a thin adapter over a remote dispatch).
  - Exported from the top-level package: `from adk import CompletionGate, gated_run`.

## [2.19.0] - 2026-07-12

### Added — Eve agent importer + durable A2A tasks + canonical agent discovery

- **`adk pack import <eve_agent_path>`** — convert external Eve agents to AitherADK packs
  (from GitHub or local filesystem). Maps Eve structure (instructions → system prompt,
  agent.ts → agent.yaml, skills/*.md → skills, connections/*.ts → MCP, tools/*.ts →
  tools/node verbatim) to AitherADK format with a single command. Eve agents (compiled
  manifest format, schema v35) become installable packs compatible with `adk pack install`.
- **Durable A2A task lifecycle (`TaskManager` backed by FileStore)** — agent-to-agent
  tasks now survive container restarts. Tasks persist as append-only JSONL
  (`.adk/tasks.jsonl`); in-memory dict for fast access; `create_task()` / `update_status()` /
  `add_artifact()` / `add_message()` all save to disk. Google A2A v0.3.0 spec compliant.
- **Canonical agent card discovery** — `A2AServer.mount()` now exposes:
  - `GET /.well-known/agent-card.json` — Agent Card (canonical; contains agent identity,
    capabilities, skills, endpoints)
  - `GET /.well-known/agent.json` — Agent Card (legacy, redirects with 308 to canonical path)
  - `POST /a2a` — JSON-RPC 2.0 for message send/task lifecycle
  - `GET /a2a/tasks/{id}/subscribe` — SSE streaming task updates
- **Four built-in agent packs now complete and catalogued**:
  - `hermes` — architecture review & trade-off analysis
  - `openclaw` — web research & source verification
  - `claude-code` — feature development & debugging
  - `analyst` — structured data inference & anomaly detection
  All discoverable via `adk packs` / `adk install pack:<name>`.
- **Tolerant tool-call JSON parsing** — malformed tool JSON (extra properties, missing
  name/arguments, string-encoded nested args, missing keys) is now gracefully degraded
  instead of silently failing. `_parse_tool_json()` accepts both `"arguments"` and
  `"parameters"` keys, coerces string-encoded args, and skips unparseable calls instead
  of crashing the turn.

### Known limitations

- Eve agent importer: remote GitHub fetching not yet implemented (local paths only).
- Durable task store: currently JSONL (suitable for thousands of records, not millions).

## [2.18.0] - 2026-07-10

### Added — AitherShell command center + Claude Code session management

- **`aither sessions`** — Claude Code session manager: interactive full-screen
  browser (type-to-filter, transcript preview, deep full-text `search`, resume
  **in the current terminal** or as Windows Terminal tabs), crash guard
  (`guard install` = at-logon watchdog that snapshots live sessions and
  auto-restores the whole set after a terminal crash), and `ingest [--watch]`
  (incremental, secret-guarded sync of session conversations into the local KB
  / CompanyBrain via the standard ingest pipeline).
- **`aither hq`** — command-center dashboard: per-service fleet health, LLM
  queue (depth/VRAM/models), Pulse alerts, live session count, inbox unread —
  auto-refreshing, with one-key jumps into chat, sessions, inbox, agents,
  brief, watchtower, and docker recovery.
- **`aither inbox`** — unified queue over CommCore mail, Relay
  mentions/notifications, and Pulse alerts; open/mark-read/DM-reply from the
  terminal. Every source degrades independently.
- **`aither agents`** — roster console + `agents ask -e <1-10> <agent> <q>`
  (effort-tiered ask via Genesis, with automatic `/chat/stream` fallback when
  the `/agent/sync` execute gate rejects host callers) + forge dispatch
  inspection + routine health.
- **`aither palette`** — universal fuzzy picker across actions, sessions, and
  services; **`aither brief`** — renders Atlas's executive briefing in the
  terminal; **`aither watch`** — fleet watchtower with named wedge-signature
  detection (docker-WSL wedge, LLM-queue stall, crash-loop uptime regression)
  and optional docker auto-recovery.
- All fleet reads go through a shared fail-soft `FleetClient` (internal-CA
  TLS via `adk._tls`, dashboard-grade timeouts, per-source degraded states).
- Node shell-cli (`@aitherium/shell-cli` 1.13.0): `aither
  sessions|hq|inbox|palette|brief|watch|agents|docker` now pass through to the
  Python shell instead of being intent-classified as chat prompts.

## [2.17.1] - 2026-07-05

### Fully self-service mesh join — no customer key handling

- **Auto-join headscale off the onboard response.** The conductor now auto-issues a
  headscale pre-auth key for NAT'd nodes and returns it in the `/v1/mesh/onboard`
  response; `mesh.join` consumes it and brings up the tunnel automatically. A customer
  runs one command — they never mint, fetch, or set a headscale key. Explicit
  `--headscale-key`/`AITHER_HEADSCALE_AUTH_KEY` still take precedence.
- **Security:** `_tailscale_up` now redacts the auth key from all logs and exceptions
  (a failed `tailscale up` previously echoed the full argv, leaking the key into logs).

## [2.17.0] - 2026-07-05

### Added — Headscale mesh transport + local Qdrant + Awconnect onboarding

- **Headscale NAT-friendly mesh transport.** When raw WireGuard UDP:51820 is not
  viable (customer boxes behind NAT, corporate firewalls, CGNAT), `adk mesh join
  --headscale` routes the mesh tunnel through Tailscale's Headscale control plane.
  The overlay IP is still Conductor-assigned (10.77.0.0/16); Headscale provides
  only the transport layer. Configured via `AITHER_MESH_TRANSPORT=headscale`,
  `AITHER_HEADSCALE_URL` (default: https://headscale.aitherium.com), and
  `AITHER_HEADSCALE_AUTH_KEY` (pre-generated key). Automatic fallback to raw
  WireGuard if Headscale setup fails.
- **Local Qdrant vector DB provisioning.** `adk stack` now provisions Qdrant
  locally when `AITHER_VECTOR_DB=qdrant` is set, enabling offline-first RAG
  without external dependencies. Tiered: local Qdrant in-fleet, cloud Nexus
  for enterprise multi-tenant deployments.
- **Conductor URL public fallback.** `adk mesh join` resolves the internal
  `aitheros-conductor:8193` hostname; if unreachable (e.g., self-hosted nodes),
  it falls back to the public `conductor.aitherium.com:8193` endpoint,
  eliminating bootstrap configuration friction.

### Documentation

- **Self-hosting runbook.** New `docs/SELF_HOSTING.md` covers deploying AitherOS
  on customer infrastructure with Qdrant + mesh + Awconnect onboarding.
- **Mesh transport constraints documented.** Raw WireGuard requires public
  UDP:51820 endpoint (cloud instances, dedicated hardware); Headscale for
  NAT'd networks. Noted in CLI help and module docstrings.

## [2.15.0] - 2026-07-04

### Added — built-in web admin console + portal-profile settings sync

- **Web admin console.** The page `adk up` opens (served at `/` and `/ui`) is now a full,
  self-contained console — Chat plus six admin tabs: **Backend** (view/switch LLM provider,
  set keys, test connection), **Packs** (list/enable/disable/reload tool packs),
  **Sessions** (browse/delete conversations), **Logs** (redacted agent-log tail),
  **Graph** (knowledge-graph search/neighborhood/stats), and **MCP** (add/remove external
  MCP servers). No build step, no CDN — a single packaged HTML asset. The original minimal
  streaming chat page stays at `/chat` for back-compat.
- **Admin API (`adk/admin_api.py`).** ~20 endpoints under `/admin/*`, all bearer-gated by the
  existing middleware (never in `_skip_auth_paths`). Operates on the live agent — LLM backend
  swap (`LLMRouter.switch_backend`), pack reload, and MCP registration take effect without a
  restart.
- **Settings sync (`adk/settings_sync.py`).** The user's portal profile is the source of
  truth: on startup the agent pulls `preferences.adk` and applies it over the local
  `~/.aither/config.yaml` cache; admin-console edits push a debounced snapshot back to
  `PUT /api/settings/preferences`. Provider API keys and MCP auth headers are device-local
  secrets and are never included in the pushed snapshot. Opt out with `AITHER_SETTINGS_SYNC=false`.

### Security

- **Timing-safe token comparison.** The server bearer check now uses `hmac.compare_digest`
  (the token is reachable over the public tunnel; a byte-wise `!=` leaked it to timing attacks).
- **MCP add is SSRF-guarded + confirmed.** Adding an external MCP server is a two-step
  prepare→confirm flow; URLs resolving to loopback / private / link-local / metadata addresses
  are rejected (override with `AITHER_ALLOW_PRIVATE_MCP=1` for trusted local servers).
- **Config editor is allowlisted.** `PATCH /admin/config` writes only safe scalar prefs and
  hard-denies fields that could execute code or redirect traffic at startup (e.g.
  `aither_toolpack_dirs`, backend base URLs).
- **Log tail is scrubbed.** Bearer tokens, `#k=` fragments, and `sk-`/`aither_sk_` keys are
  redacted from the log-tail endpoint; the tunnel log is excluded.

## [2.14.8] - 2026-07-04

### Added — `adk up` for normal humans (no API key, no prereqs, live chat)

- **Hosted-brain default — no API key required.** When there's no local model and no
  provider key, `adk up` signs in (device-flow) and routes inference through the gateway
  using the portal token (`AITHER_LLM_BACKEND=gateway` + bearer). A non-technical user
  never needs to obtain or paste an LLM API key. Explicit `--provider`/local model still
  take precedence; unattended `--yes` with no token/backend fails cleanly (exit 3).
- **cloudflared auto-download.** If the tunnel binary is missing, `adk up` downloads the
  right platform asset to `~/.aither/bin` (SHA256-verified against the release; opt out with
  `AITHER_CLOUDFLARED_REQUIRE_CHECKSUM=0`) instead of exiting with an install hint.
- **Built-in streaming chat page.** The agent now serves a self-contained chat UI at `/`
  (and `/chat`) with a live "thinking…" indicator and token-by-token streaming — so a human
  has somewhere to talk to it with immediate feedback instead of a blank wait. `adk up` opens
  it locally and passes the callback bearer in the URL fragment (`#k=…`), so `/chat/stream`
  stays authenticated (not an open proxy) with nothing to paste.
- **Human-readable failures.** `adk up` errors now carry a plain-English `next_action`
  (kept machine-readable in `--yes` JSON).

### Fixed

- `adk up` chat link pointed at a non-existent `/settings/agent` route; it now points at the
  agent's own streaming chat page.

## [2.14.7] - 2026-07-04

### Added

- **`adk up` — one command to run a persistent, fleet-connected agent.** Collapses
  the scattered onboarding surface (`host`/`quickstart`/`connect`/`login`/`setup`)
  into a single command that starts `aither-serve` (single agent, identity `aither`
  by default), opens a Cloudflare quick-tunnel for fleet reachability, registers with
  the portal, and installs a reboot-persistent autostart entry (Windows Task Scheduler
  / systemd-user / launchd). Detaches by default so the terminal is freed; `--foreground`
  blocks. Reuses the existing device-flow login, cloudflared discovery, provider-key
  store, and `_preflight_check` backend detection.
- **Fully non-interactive path for autonomous agents.** `adk up --yes` (or any
  non-TTY invocation) takes ZERO prompts — everything resolves from flags, env, and
  saved config — and emits a single machine-readable JSON status line with meaningful
  exit codes (`0` ok, `2` bad-input, `3` no-backend, `4` tunnel-missing, `5` register-failed).
  Degrades to local-only (warn, not fail) when a portal token is absent unless
  `--require-register` is set.
- **`adk down` / `adk status --json` companions.** `down` stops the agent + tunnel,
  best-effort deregisters, and removes the autostart entry; `status` reports the running
  agent (liveness + `/health` + tunnel + registration), with `--json` for agents/CI.
- **`adk stack`** — the previous `adk up` behaviour (supervise the Room + Ollama consumer
  stack) now lives here; `adk up` is the connected-agent command.

### Internal

- New `adk/agent_daemon.py`: detached process spawn, status file
  (`~/.aither/adk-up.json`), pid liveness/teardown, and cross-platform autostart install.

## [2.14.6] - 2026-07-04

### Added

- **`adk.preflight` — self-bootstrap capability probe.** `run_preflight(agent,
  spec)` wakes an agent up knowing what it is actually plugged into: a bounded
  (1.5s) parallel `LivenessProbe` over the router's resolved backends
  (primary/reasoning), the embeddings winner, remote MCP tools, the structured-ML
  / `/ml/teach` endpoints, and voice — returning an honest `CapabilityReport`.
  Status vocabulary distinguishes reachability from entitlement
  (`OK`/`MISSING`/`UNREACHABLE`/`AUTH`/`TIER_DENIED`/`UNSUPPORTED`), a hosted slot
  never prints a bare `OK`, and a required-but-unsatisfied slot aborts before the
  loop instead of blind-firing. Headless task/rules resolution + an allowed-roots
  policy binding (`builtin_tools.set_allowed_roots`, which resets the memoized
  cache so the override actually enforces).
- **Multimodal (image) messages.** `Message.content` now accepts an OpenAI-style
  content-part list (`str | list[dict]`), passed through verbatim to the
  OpenAI-compatible body (gateway / vLLM / OpenAI). New `adk.llm.multimodal`
  helpers — `image_message()`, `image_content_parts()`, `to_image_url()` — build
  the parts and encode local image bytes/paths into `data:` URIs.
- **Real vision preflight probe.** The vision slot is no longer unconditionally
  `UNSUPPORTED`: it sends a discriminable image (a solid red square) and requires
  the model to name the color, so a blind/text-only model cannot false-pass. `OK`
  only on a real round-trip; `AUTH` on 401/403; `UNSUPPORTED` when there is no
  OpenAI-compatible provider or the model cannot see the image.

## [2.14.5] - 2026-07-04

### Added

- **`adk.reasoning.mcts` — generic Monte-Carlo Tree Search library.** A reusable
  MCTS engine (ported from the internal `UnifiedMCTS`, with all internal couplings
  removed) that any agent can drive over its own environment:
  - `MCTSEnvironment` protocol (`get_state_hash` / `get_actions` / `step` /
    `evaluate` / `clone`) — implement it and search.
  - Three **optional, default-off** model seams — `TransitionModel`, `PolicyModel`
    (PUCT priors), `ValueModel` — so a learned world-model / policy / value can be
    injected without touching the core; with all three `None` the engine is a plain
    UCT search.
  - `ObservedTransitionModel` adapter — a cheap online lookup world-model (hit =
    exact stored transition; miss = identity + negative "unknown" reward +
    `is_uncertain`, biasing exploration toward learning), with JSONL save/load for
    cross-run reuse.
  - `UnifiedMCTS(config).search(env)` returns an `MCTSResult` (best action, value,
    confidence, principal-variation path) and an optional `MCTSTrace`
    (visit-distribution policy target) for distillation.
- **Self-bootstrapping agent machinery.** Build an agent from a declarative spec
  instead of bespoke wiring:
  - `adk.bootstrap.build_agent_from_spec(path) -> (AitherAgent, RunCtx)` — resolves
    prompts, activates required/optional tool packs, and carries a `memory_map`
    (a `node_type -> (role, tier)` override for memory writes, never a gate).
  - `adk.prompts.PromptBridge` — resolves an agent's prompt bindings from files
    (relative to the spec) or `AitherPrompts` dot-keys (optional import).
  - `adk.packs.PackActivator` — eager activation of installed tool packs by name
    over `register_tool_packs`; a required pack that registers zero tools raises
    `PackUnavailable`, optional packs soft-degrade.
  - `python -m adk.bootstrap.generic_agent <spec.yaml>` inspects a spec (resolved
    name, activated packs + tool counts, prompt keys, memory map) without running a
    live loop.
- **`deep_research` tool pack** — a full multi-engine web-research workflow as
  `dr_*` agent tools (keyless search, page fetch + cache, site mirror, a citable
  findings knowledge graph, and markdown/PDF/DOCX report generation), activatable
  by name via the tool-pack loader.

## [2.14.4] - 2026-07-04

### Added

- **`structured_ml` tool domain** — zero-shot structured-data inference for agents,
  backed by TabFM (tabular classification/regression) and TimesFM (time-series
  forecasting). Three tools:
  - `tabular_classify(support_rows, target, query_rows)` — classify rows from a
    labeled support set, in-context (no training step; up to 10 classes).
  - `tabular_regress(support_rows, target, query_rows)` — predict a numeric target.
  - `timeseries_forecast(series, horizon)` — forecast future values of a series.

  The tools POST to a structured-ML inference service resolved from
  `AITHER_STRUCTURED_ML_URL` (default `http://localhost:8192`), reject non-http(s)
  URLs, and summarise oversized responses to protect the agent's context.
- **`Capability.STRUCTURED_INFERENCE`** capability token and a new **`analyst`**
  identity/pack that ships the `structured_ml` domain by default. The domain is
  opt-in — only identities that enable the `structured_ml` category get the tools.

## [2.14.3] - 2026-07-03

### Fixed

All four bugs below were found by actually booting `adk run` against a real
chat model and driving a real tool-calling request end-to-end (not inferred
from reading the code) — each one silently broke the "local agent uses its
configured model and its tools actually work" path.

- **`AITHER_MODEL` / `config.model` silently ignored by the OpenAI-compatible
  backend**: `LLMRouter.__init__` never read `config.model` into `self._model`
  unless the caller passed `model=` directly to the constructor — which nothing
  did. Every explicit model choice (env var, config file) was dropped, the
  provider fell back to a hardcoded per-backend default (`gpt-4o-mini` for the
  `openai` backend), and an unrecognized model name got silently remapped
  server-side to the wrong backing model. Concretely: setting
  `AITHER_MODEL=aither-orchestrator` had no effect — every request actually
  went to a different model that was never meant to be called directly and
  can't reliably use tools.
- **`OPENAI_BASE_URL` ignored for explicit `--backend`/`AITHER_LLM_BACKEND`
  selection**: `LLMRouter._auto_detect()`'s explicit-backend branch hardcoded
  `base_url=None` when constructing the provider, instead of reading
  `config.openai_base_url` (which two other call sites in the same file
  already did correctly). Any self-hosted OpenAI-compatible endpoint
  (MicroScheduler, vLLM, LM Studio) configured via `OPENAI_BASE_URL` was
  silently ignored in favor of `https://api.openai.com/v1`.
- **Tool-call steering never engaged on default-effort chat**: the retry that
  nudges a model back into structured tool-calling when it responds with prose
  instead of a function call was gated behind `effort >= 6`. Default `effort`
  for a plain `agent.chat(message)` call resolves to `5`, so the safety net —
  built specifically for this failure mode — never fired for default requests.
  Also now forces `tool_choice="required"` on the retry instead of relying on a
  text nudge alone.
- **Every non-string builtin-tool parameter silently typed as `"string"` in
  the generated tool schema**: `builtin_tools.py` uses
  `from __future__ import annotations`, so `_extract_parameters()` in
  `tools.py` was reading postponed (string) type hints (`"int"`, not `int`)
  straight out of `fn.__annotations__`, which never matched
  `_type_to_schema`'s type-object keys and silently fell through to
  `{"type": "string"}`. Every builtin tool with an `int`/`float`/`bool`/`list`
  parameter was affected — e.g. `file_read(start_line: int, end_line: int)`
  advertised both as strings, so a model correctly calling the tool with
  `"start_line": "0"` crashed the actual implementation with
  `unsupported operand type(s) for -: 'str' and 'int'`. Fixed by resolving
  hints through `typing.get_type_hints()` instead of the raw annotations dict.
  `_coerce_arguments()` — the runtime safety net that's supposed to coerce a
  model-supplied `"0"` string into `0` before a tool call executes — had the
  exact same raw-`__annotations__` bug and silently no-opped for every builtin
  tool; fixed the same way.
- **Streaming tool-loop could hang a connection forever**: `chat_stream()`'s
  fallback to the sync tool loop (tool calls can't stream token-by-token) had
  no timeout, so a model that didn't cleanly terminate a tool-calling round
  trip left the SSE connection open indefinitely with zero bytes sent. Bounded
  with a 60s `asyncio.wait_for`; on timeout the stream yields a clear message
  instead of hanging.

## [2.14.2] - 2026-07-03

### Fixed
- **AitherShell session amnesia**: the REPL generated a fresh `session_id` for
  EVERY message, so conversation history never followed across turns (the
  server stored each turn under a different session). One stable id is now
  minted per shell run; `/resume` and `/new` semantics unchanged, and the
  cross-restart auto-restore of the last session now actually works.

## [2.14.1] - 2026-07-02

### Added
- **Account-tied licensing via device-flow login** — `adk login` now persists the account
  license returned by the device-flow handshake to `~/.aither/license.json` (base64
  `license_key` → `{payload, signature}` envelope), so entitlements from the authenticated
  account carry into every later `adk` invocation without re-auth. Pairs with the fleet-side
  node tool-pack gate that grants packs by plan-tier rank.
- **`can_use_formbridge` / `can_use_untether` entitlements** (PROFESSIONAL+ rank ≥ 3) backing
  the FormBridge and UNTETHER agent tool-packs.

### Fixed
- `adk` CLI: guard the optional `memory_graph` singleton (was a latent `NameError` on the
  `/stats`, `/memory`, and save paths when memory was not constructed).

## [2.14.0] - 2026-07-02

### Added
- **`adk.memory_wiki` — LyraWiki-style self-managed semantic knowledge** (opt-in; nothing constructs it by default): `MemoryWiki(graph_memory)` maintains curated wiki ARTICLES as `wiki_article` nodes (title/slug, LLM-owned markdown with `[[wikilinks]]`, embedding, durable tier, `CONSOLIDATED_FROM` edges to source memories, governance-ledgered revisions).
  - `consolidate(llm, since=, budget=)` clusters unconsolidated episodic/fact nodes (embedding + keyword), drafts/UPDATEs articles via the INJECTED llm callable (`str -> str` or `messages -> str`, sync or async — no hard model dependency), cites source node ids, marks contradicted sources superseded, then DEMOTES consolidated sources to a fast-decay tier via `promote()` (represented knowledge doesn't need raw copies).
  - `lint(llm=None)` — deterministic health checks (orphan wikilinks, empty/stale articles, live-but-contradicted pairs) + an optional LLM audit pass.
  - `prune(relevance_floor=0.05, hard_delete_after_days=14)` — article relevance = tier freshness × reinforcement (reinforce-on-recall) + link-degree to LIVE memories; below the floor → reversible governance tombstone; entombed past the window → **HARD-DELETE** (the tombstone snapshot itself is purged — content and embedding irrecoverable; the true-deletion path). A node is never deleted without a tombstone first.
  - `recall(query, limit)` — article-first RAG (curated articles rank above raw nodes) that reinforces returned articles.
- **`adk.routines` — agent heartbeat + self-programmed routines**: `RoutineStore` is a durable registry (`~/.aither/routines/{agent}.json`, `AITHER_DATA_DIR`-aware, injectable path) of cron-scheduled self-prompts `{name, cron, instruction, enabled, last_run, last_result, tags}` that rehydrates into the existing `adk.cron.CronScheduler` on `start()`. A fire runs `agent.chat(instruction)` with the agent's full toolset — the agent programs its future self with its own adk. Guardrails: `max_routines` (12), a 5-minute min fire interval per routine, a per-fire timeout, and every result ledgered (truncated) to `last_result`. Direct-callable routines (`register_direct`) fire a bound method instead of a self-prompt. Self-management tools (`routine_create/list/update/pause/resume/delete/run_now`) ship as OpenAI tool defs + handlers (`build_routine_tools` / `register_routine_tools` / `routine_tool_defs`); the handlers ONLY touch the RoutineStore — they can never modify agent config or safety-relevant settings (the leash principle).
- **`AitherAgent(routines=True, memory_maintenance=True)`** (both default **False** → the default agent is byte-identical): `routines=True` builds the RoutineStore + scheduler, registers the self-management tools, and starts the heartbeat lazily on the first `chat()` (or via explicit `start_routines()`). `memory_maintenance=True` registers DEFAULT routines — `wiki_consolidate` (every 2 h), `wiki_lint` / `wiki_prune` / `graph_sweep` (daily) — as DIRECT method fires so memory upkeep runs even on tiny models, while staying visible/manageable via `routine_list`.
- `TombstoneStore.purge(tombstone_id)` — the hard-delete endpoint: irrecoverably removes a tombstone snapshot and rewrites the persisted store without it (consumed by `memory_wiki.prune`).

## [2.13.6] - 2026-07-02

### Added
- **Qdrant backend for GraphMemory dataplane sync — TRUE two-way, verified live.** Set `AITHER_FLEET_QDRANT_URL` and the sync uses Qdrant (`upsert` + `scroll` + filtered `search`) instead of Nexus. This is what actually delivers cross-agent memory sharing: a node is upserted with its OWN embedding under a deterministic per-(tenant,node) point id (idempotent re-push), and `fleet_pull` reliably SCROLLS a tenant's points back — the enumerate Nexus lacks (its `/search` doesn't return ingested docs and `/export` needs lancedb, which has a hard dependency conflict in the fleet). **Proven live with 2 agents:** agent-1 ingests → auto-syncs to the tenant dataplane; a fresh agent-2 in the same tenant `fleet_pull`s all of agent-1's nodes and can search them; a different tenant pulls nothing (isolation). Dimension-safe (never mixes vector dims in a collection). Local SQLite stays source of truth; best-effort, never raises.

## [2.13.5] - 2026-07-02

### Fixed
- **CRITICAL — GraphMemory dataplane sync push was 100% failing.** `_fleet_push_node` sent `source_type="graph"` / `content_type="graph_node"`, which AitherNexus rejects as invalid enums (HTTP 500) — so NO graph node ever replicated in 2.13.4. Now sends the accepted `source_type="manual"` / `content_type="text"` (graph provenance is carried in `metadata.synced_from`). **Verified live** against a fleet Nexus: a 4-node ingest replicates with `pending==0` and the collection count reflects it.

### Added
- **Swarm-awareness notification** — on a successful sync batch, `GraphMemory` emits a best-effort `graph.synced` event (tenant/workspace/agent/collection/count) to `AITHER_FLEET_EVENTS_URL` so OTHER agents in the tenant/swarm can pull the fresh data and deconflict in-flight work (push-based awareness instead of polling). No-op unless the env is set.

### Known limitation
- Cross-agent **rehydration** (`fleet_pull`) is only reliable when the fleet Nexus is backed by lancedb (persistent + exportable). An in-memory Nexus stores on `/ingest` but neither its `/search` nor `/export` returns the docs — two-way read needs the persistent Nexus.

## [2.13.4] - 2026-07-02

### Added
- **Memory-maintenance primitives on `GraphMemory`** (all opt-in; default behaviour byte-identical):
  - **Reinforce-on-recall**: `recall_with_activation(..., reinforce=True)` bumps `reinforcement_count`/`last_reinforced` on every RETURNED node's metadata mirror (one UPDATE per node, WAL-safe) — making the `MemoryRecord` reinforcement fields the activation scorer reads LIVE. Default `False` = zero writes.
  - **`sweep(now, archive_below=0.05, max_nodes=None, dry_run=False)`**: read-time decay enforced at rest — archives nodes past their tier TTL with freshness×reinforcement score below `archive_below` and `reinforcement_count <= 1`; `max_nodes` additionally archives the lowest-scored overflow. PERMANENT tier immune. Every archive is a reversible governance tombstone (`TombstoneStore.recover`) + a FORGET ledger entry; a node is never deleted without a tombstone. `dry_run` returns the would-archive list without acting.
  - **`promote(node_id, tier=?, role=?)`**: re-tier/re-role a node IN PLACE (tier/role columns + metadata mirror) — edges, embedding and ledger history survive; ledgers an UPDATE entry when governance is on.
- `MutationType.UPDATE` ledger entry kind (in-place re-tier/re-role).
- **Per-tenant dataplane sync for `GraphMemory`** — replicate the local graph to a per-tenant vector dataplane (Nexus/Qdrant) so a sovereign agent's memory is durable + rehydratable across restarts and hosting boundaries. `fleet_push_all_nodes()` (catch-up), `fleet_pull()` (best-effort semantic top-up), `fleet_sync_pending()`, and auto-sync on ingest (`_maybe_autosync` + `drain_sync()`). **On by default** (`AITHER_FLEET_SYNC=auto`; only `false/0/off` disables) with an inferred target (local Nexus `:8122` or the gateway in cloud mode) and a SEPARATE collection (`AITHER_FLEET_GRAPH_COLLECTION`, default `graph_memory`) from the KV memory sync. New `GraphMemory(tenant_id=, workspace_id=, fleet_url=, auto_sync=)` constructor args override env (required for multi-tenant host processes). Node `synced` column + schema v4 migration. TLS verify is CA-bundle-aware (`AITHER_CA_BUNDLE`) for the internal-TLS mesh. Local SQLite stays the source of truth; every sync path is best-effort and never raises into ingest.

## [2.13.3] - 2026-07-02

### Added
- **Canonical self-deploying embeddings provider** (`adk.embeddings`): one embedding provider for the whole SDK so vectors are portable across scopes (platform / tenant / sovereign) — same model + dimension everywhere. Pinned to `nomic-embed-text` (768-d; the vLLM `--served-model-name` for nomic-embed-text-v1.5). Lazy, single-flight resolution chain: explicit `AITHER_EMBEDDINGS_URL` → local vLLM (`:8209` then `:8120`, HTTPS-then-HTTP) → local Ollama → gateway (`AITHER_GATEWAY_EMBEDDINGS_URL`) → auto-deploy a local vLLM embeddings container if a GPU + Docker are present (opt out with `AITHER_EMBED_AUTODEPLOY=0`) → CPU sentence-transformers (384-d, degraded) → feature-hash (384-d, degraded, always works). Every batch is dimension-tagged so callers never silently mix 768-d and 384-d vectors. `get_default_embedder()`, `embed_texts()`, `embed_one()`, `get_provider()`, `reset_provider()`.

### Changed
- **`GraphMemory` now defaults its embedder to the canonical provider** (opt out with `AITHER_GRAPH_EMBEDDER=legacy`), so every adk agent shares one 768-d space. Added a meta-table dimension guard: the index is pinned to its first embedding's dimension and later different-dimension vectors are refused (kills 768↔384 mixing that silently poisons cosine similarity).

## [2.13.2] - 2026-07-01

### Added
- **Session persistence**: AitherShell now auto-saves the active session_id to `~/.aither/config.yaml` after each chat turn and auto-restores it on the next shell launch. Multi-turn conversations survive across restarts — the messages were always persisted in SQLite + JSON, only the session pointer was missing.
- `/new` command to start a fresh session (clears saved session_id).
- `/sessions` command to list recent sessions with agent name, message count, and timestamp. Resume any with `/resume <id>`.

## [2.13.1] - 2026-07-01

### Fixed
- **CRITICAL:** `deploy_connect()` now sends a HEAD request to verify the GitHub release asset exists before downloading, with a clear error if the asset is missing.
- **CRITICAL:** Compose file URLs are now pinned to a stable commit hash (configurable via `AITHER_COMPOSE_PIN` env) instead of tracking the mutable `main` branch.
- **CRITICAL:** `deploy_adk_node()` now falls back to starting `adk-serve` natively when Docker is unavailable, instead of blocking with "Docker not installed".
- **HIGH:** GHCR login failure now blocks deployment immediately with actionable guidance, instead of warning and continuing to a confusing image-pull failure.
- **HIGH:** Health checks now stream the last 30 lines of Docker Compose logs on failure, so the user can see why a container didn't start.
- **HIGH:** vLLM port allocation now probes for conflicts before assigning; if port 8200 is busy, workers are automatically remapped to the next free port.
- **MEDIUM:** Compose file downloads are now cached with ETag; re-running `adk deploy node` skips re-downloading an unchanged file.
- **MEDIUM:** DGX Spark discovery now respects `AITHER_DGX_HOSTS` env (comma-separated) for custom network topologies, instead of only checking `spark.local` and `192.168.0.33`.
- **MEDIUM:** Cloud API keys (Anthropic, OpenAI, DeepSeek) are now validated with a lightweight probe during `adk setup` infra scan, so invalid keys surface early.
- **MEDIUM:** `deploy_connect()` now validates the extracted extension has a `manifest.json` and warns if it's missing.

## [2.13.0] - 2026-07-01

### Removed
- **`adk.faculties.memory_graph`** — the pickle-based `MemoryGraph` module is deleted. `from adk.faculties import MemoryGraph` now resolves to the canonical `adk.graph_memory.GraphMemory` for back-compat.
- **`adk.platform.memory`** — the entire legacy memory subsystem (`MemoryManager`, `UnifiedMemorySystem`, `GameEngine`, `StoryboardEngine`, `AnchorGenerator`) is deleted. Agent memory is `adk.memory.Memory` + `adk.graph_memory.GraphMemory`.

### Changed
- `adk work` command now uses `GraphMemory` instead of the removed pickle-based faculty graph.
- `adk.MemoryGraph` and `adk.faculties.MemoryGraph` lazy exports now resolve to `adk.graph_memory.GraphMemory`.
- `agent.set_memory_graph()` kept for back-compat but documented as deprecated; agents already wire `GraphMemory` automatically in `__init__`.
- Platform startup (`adk.platform.infrastructure.startup`) gracefully handles missing `MemoryManager`.
- Platform CLI module listing and health check updated to reflect removed memory module.

## [2.12.7] - 2026-07-01

### Deprecated
- `adk.faculties.MemoryGraph` — emits `DeprecationWarning` on instantiation; use `adk.graph_memory.GraphMemory` (SQLite-backed, governed, embedding-native) instead.
- `adk.platform.memory` package — the legacy `MemoryManager`, `UnifiedMemorySystem`, `GameEngine`, and `StoryboardEngine` modules now emit `DeprecationWarning` on import; use `adk.memory.Memory` and `adk.graph_memory.GraphMemory` for new code.

### Changed
- Updated `adk.faculties` docstring and usage examples to point at the canonical memory surface.

## [2.12.6] - 2026-07-01

### Changed
- Trimmed public package debt by removing grid runbook artifacts and shell/room binary placeholder directories from published artifacts.
- Removed stale public-sync allowlist entries for deleted phase test files and added explicit public-sync guards for binary placeholder directories and grid artifacts.
- Updated the public README grid note so it no longer links to runbook files excluded from the SDK package.

## [2.12.5] - 2026-07-01

### Added
- Added best-effort fleet memory sync from local ADK memory into Nexus/Qdrant-compatible RAG stores, including batch catch-up and vector-search recall hooks.
- Added saved config/env support for fleet memory URL, collection, and sync mode.

### Changed
- Excluded FormBridge runtime/test internals from public ADK packaging and public repo sync.

## [2.12.4] - 2026-07-01

### Added
- Added a hot-reload endpoint for installed agent packs so newly applied pack tools can be picked up without restarting `aither-serve`.

### Fixed
- Fixed synchronous `ChatRelay` calls on Python 3.12 when no event loop is running; async WebSocket delivery is still scheduled when a loop exists.
- Aligned gateway inference tier tests with the current public ladder: free, starter, pro, enterprise, platform.

## [2.12.0] - 2026-06-28

### Added — one-command local inference + upgrade path (click-to-run)
- **`adk quickstart-local`** — detects hardware, picks a backend
  (Ollama / llama.cpp / vLLM), installs it, downloads a model, verifies, and
  prints next steps. Bootstraps Ollama itself via winget/brew/install.sh when
  missing (no "go install it" dead-ends). Default model `gemma4:e2b`.
- **`adk backend switch <ollama|llamacpp|vllm>`** (+ `backend status`) — migrate
  the active deployment between engines; re-points config and smoke-tests.
- **`adk install pack:<name>`** (+ `adk install list` / `adk packs`) — install
  ready-made agent packs (openclaw, hermes, claude-code) into `~/.aither/agents`.

### Fixed (found via live end-to-end testing on Windows + CUDA)
- quickstart-local reported success on a dead endpoint (smoke test was warn-only);
  a failed smoke now fails the command, with a service-readiness wait.
- `--model` resolved to `None` (getattr default never fired) → uses the real default.
- Smoke test: 30s→180s timeout for cold model load; accepts reasoning-model output
  (empty `content` at low token budgets is no longer a false failure).
- `adk install pack:<name>` colon syntax (argparse rejected it before dispatch).
- `adk backend status` health probe used a nonexistent `/health` path → 404; now
  probes `/v1/models` (uniform OpenAI-compatible liveness for all three backends).
- Windows: removed non-ASCII glyphs from printed strings that crashed the cp1252
  console (a `✓` even made a successful install report failure).

## [2.11.1] - 2026-06-14

### Fixed — `adk host` / `adk login` device flow (autonomous onboarding)
Found driving `adk host` against production; all broke real device-flow login:
- **Device flow hit the portal FRONTEND, not AitherIdentity.** `adk login`/`adk host`
  POSTed `/auth/device/code` to portal/veil.aitherium.com, which 307-redirects API auth
  to `/login`. Added `_resolve_identity_url()` mapping the aitherium.com topology to
  `idp.aitherium.com` (the Identity API); localhost/bespoke pass through unchanged.
- **Cloudflare 403'd urllib.** `idp.*` sits behind Cloudflare, which blocks urllib's
  default `Python-urllib/3.x` UA. Device-code + poll requests now send a real
  User-Agent + Accept (verified urllib→idp 200, was 403).
- **`adk host` now self-authenticates via device flow** when there's no token, and
  **retries via device flow on a 401/403** (a stale saved token no longer dead-ends
  registration).

## [2.11.0] - 2026-06-14

### Added — attach your own MCP "hands" to a self-hosted agent
A self-hosted agent now has the full brain (your model) · body (this loop) · **hands**
(your tools) trifecta. The AitherOS platform relays the MCP servers you registered for
your agent in the `/stream` request body as `mcp_endpoints`, and the loop wires their
tools into the ReAct turn:
- **`adk/mcp_endpoint_tools.register_mcp_endpoint_tools(agent, endpoints)`** — speaks MCP
  JSON-RPC (`tools/list` to discover, `tools/call` to invoke) and registers a namespaced
  proxy tool (`{endpoint}__{tool}`) for each advertised tool. Mirrors `app_proxy_tools`.
  Best-effort: an unreachable endpoint registers nothing and never raises.
- **`POST /stream` and `POST /chat/stream`** accept an optional `mcp_endpoints` list
  (`[{name, url, [headers]}]`); `_aitheros_stream` attaches them to the agent before the
  turn. Existing callers are unaffected (the field is optional).

## [2.10.1] - 2026-06-14

### Fixed — self-hosted `aither-serve` was crash-on-startup (2.10.0 regression)
A fresh `pip install aither-adk && aither-serve --backend deepseek` could not boot. Found
and fixed while standing up a real self-hosted DeepSeek agent end-to-end:
- **Startup `NameError: name 'port' is not defined`** in three lifespan helpers
  (`_join_aithernet`, `_init_a2a_server`, `_start_mesh_hosting`) — they referenced a bare
  `port` no longer in scope. Now use `config.server_port`, and `main()` writes the resolved
  `--port`/`--host` back to `config` so every lifespan helper and the invoke_url see the
  actual bound port.
- **Fleet self-registration crashed the whole server** — the registration handlers caught
  `(ImportError, RuntimeError, OSError, ConnectionError)` but **not** `httpx.HTTPError`, so a
  `RemoteProtocolError`/connect error talking to the control plane took the agent down. All
  network-facing lifespan handlers now also catch `httpx.HTTPError` (non-fatal, as intended).
- **`--backend` was ignored when `AITHER_API_KEY` was set** — `_auto_detect` picked the cloud
  gateway before the explicit backend, and `_try_elysium_fallback` then replaced the chosen
  provider. An explicit backend (deepseek/openai/anthropic/your own vllm/ollama) now always
  wins; "auto"/"gateway" still flow through detection. The operator's own brain is never
  silently hijacked.
- **Approved-tool trace truncated after `/sessions/{id}/confirm`** — the SSE relay treated
  `AgentResponse.tool_calls_made` (a `list[str]`) as dicts (`tc.get(...)`), raised
  `AttributeError`, and an over-narrow `except` swallowed it so the client saw only
  `session_start` + heartbeats. The relay now handles string tool names, the `except` is
  broad (always emits a typed `error` + terminal `complete`), and the module-level
  `_strata_ingest` `NameError` + Chronicle `session_id` kwarg mismatch are fixed.

## [2.10.0] - 2026-06-13

### Added — human-in-the-loop tool approval (`adk/approval.py`)
- **Pause before a gated tool, resume on decision.** An agent can now pause its turn before
  executing a sensitive tool and wait for a human's allow/deny — so a managed control plane
  (or any operator) can gate tool use without forking the loop.
- **Policy via `AITHER_TOOL_APPROVAL`** — a comma-separated list of tool names (optionally
  `agent:tool`), or `*` for every tool. Empty = no gating (unchanged behavior).
- **Survives restarts / days-later approvals.** The paused turn (user message + pending tool
  calls) is persisted to disk keyed by `session_id`; resume is a fresh request, not a held
  connection. Decisions are recorded per `(session_id, tool_name)`.
- **`AgentResponse.requires_action` / `.pending`** — set when a turn paused; `pending` lists
  the gated tool calls awaiting a decision.
- **`AitherAgent.resume(session_id, decisions)`** — records the decisions and re-runs the
  paused turn (the gate consumes them: allowed tools execute, denied tools get a denial
  observation, the turn continues — and may pause again on a different gated tool).
- **Server:** `POST /sessions/{id}/confirm` records decisions and streams the resumed turn;
  `/stream` emits a `requires_action` event when a turn pauses.

## [2.9.0] - 2026-06-13

### Added — universal LLM continuation primitive (`adk/llm/continuation.py`)
- **`run_continuation()` + `stitch()`** — one shared continue-until-complete primitive. When a
  completion stops because it hit the output token cap (`finish_reason == "length"`), it continues
  the generation and STITCHES the chunks (overlap-dedup + restart-detect) into a complete answer,
  instead of every call-site hand-rolling its own retry. Bounded three ways (max rounds AND total
  output ceiling AND a no-growth break), and it never mutates the caller's message history.
- **`LLMProvider.chat_with_continuation()`** and **`LLMRouter.chat_with_continuation()`** — every
  provider AND the router inherit the primitive, so any call-site gets continuation for free.
- Kill-switch `ADK_LLM_CONTINUATION=off`; tunables `ADK_LLM_MAX_CONTINUATIONS`,
  `ADK_LLM_MAX_TOTAL_OUTPUT_TOKENS` (defaults: 2 rounds / 8192 tokens).

### Changed
- The ReAct loop's truncation recovery now uses the shared primitive. The previous inline
  doubling-retry REPLACED the partial response (losing earlier text in `resp.content`) and injected
  continuation scaffolding into the live message history; it now STITCHES and leaves history clean.
  Tool-call turns are never continued as text.

### Included — previously-committed-but-unreleased changes
- 2.8.2: TLS verification on by default (`verify=False` removed).
- 2.8.1: baked license + pack-signing public keys.
- 2.8.0: version-bump release.

## [2.7.0] - 2026-06-05

### Added — provider-agnostic LLM cost levers (at the `LLMProvider` ABC)
- **Prompt caching.** New `ProviderCapabilities` (`prompt_cache: explicit|automatic|none`, `batch`) and
  normalized `LLMResponse.cache_read_tokens`/`cache_write_tokens`, so the "tokens saved" meter never
  depends on which backend served the turn. Anthropic inserts `cache_control` breakpoints over the stable
  system+tools prefix; OpenAI/DeepSeek caching is automatic and their `cached_tokens` are now surfaced
  (previously discarded). Default-on in `AitherAgent.chat`; override with `cache=False`.
- **ResponseCache** (`adk/llm/cache.py`) — exact-match response cache with a pluggable backend, wired into
  `LLMRouter` as an opt-in (`LLMRouter(response_cache=…)` + per-call `cacheable=True`). OFF by default — no
  determinism change unless opted in.
- **Gemini provider** (`adk/llm/gemini.py`) with explicit `cachedContents` caching; registered in the router.
- **Async batch runner** (`adk/llm/batch_runner.py`) — `run_batch()` over the Anthropic Message Batches /
  OpenAI Batch APIs (~50% off) with a transparent fallback to concurrent `chat()` where unsupported.
- **Graph-RAG retriever interface** (`adk/graph_retriever.py`).

### Added — principal authority (prompt-injection defense enforced outside the model)
- `adk/auth.py`: `Principal` / `AuthContext` / `PrincipalResolver` (+ deny-all default).
- `ToolRegistry.execute(name, args, auth=…)` — a default-deny authorization gate at the tool boundary, so a
  forbidden tool call is blocked regardless of what the LLM emits. `ToolDef` gains `required_clearance` /
  `action_class`. Threaded through `AitherAgent.chat`; `auth=None` is fully backward-compatible.
- `adk/channel_auth.py`: Telegram webhook-secret + CEO-id checks and HMAC-signed approvals.

## [2.6.4] - 2026-06-05

### Fixes
- **Loop-guard nudges corrupted the message history → DeepSeek 400 mid-run.**
  When the ReAct loop emitted multiple tool calls in one assistant turn and the
  loop guard fired a WARN/BLOCK/CIRCUIT_BREAK on one of them, it spliced a
  standalone `system` nudge message *between* the tool results. DeepSeek (and
  strict vLLM chat templates) require an assistant `tool_calls` message to be
  followed by exactly one `tool` result per `tool_call_id`, contiguously — so the
  interleaved message produced `400 … insufficient tool messages following
  tool_calls message` and aborted the turn (this is reliably hit after crossing
  the 15-call circuit-break threshold, where every subsequent call WARNs).
  Per-call guidance is now folded into that call's own `tool` result, and any
  standalone steering is deferred to a single message AFTER the whole batch —
  preserving the `tool_calls`↔`tool_results` pairing every backend requires.
  Regression test: `deep-research-agent/tests/test_msg_structure.py`
  (loopguard-flood scenario).

## [2.6.3] - 2026-06-04

### Fixes
- **Transient provider errors killed a run on the first blip.** A single
  `502 Bad Gateway` / `503` / `529 overloaded` / `429` from the upstream API
  aborted the whole request — the Anthropic provider had **no retry at all**, and
  the OpenAI-compatible provider only retried `429`. Both now retry transient
  statuses (`408/429/500/502/503/504/529`) with `Retry-After`-aware exponential
  backoff (3 attempts), and surface the provider's body if all retries are
  exhausted. Non-transient `4xx` (e.g. `400`) still fails fast. Applies to
  `chat` and `chat_stream` on both providers.

## [2.6.2] - 2026-06-04

### Fixes
- **DeepSeek (and strict OpenAI-compatible backends) 400'd on long tool
  conversations.** The ReAct loop legitimately injects mid-conversation
  `system` steering messages (the "[DIMINISHING RETURNS]" hint after several
  low-output tool iterations, loop-guard nudges). OpenAI tolerates a non-leading
  `system` message; DeepSeek rejects it with a 400. Long research runs
  (many `fetch_url` calls) reliably tripped this. Fix: non-leading `system`
  messages are demoted to `user` at the OpenAI-compatible provider boundary
  (`_demote_nonleading_system`) — steering intent preserved, payload valid on
  every backend; leading system prompt(s) untouched.
- **Opaque HTTP errors.** `resp.raise_for_status()` discarded the provider's
  response body, so a 400/422 surfaced as a bare `Client error '400 Bad
  Request'` with no cause. `_ensure_ok` now includes the provider's actual
  message (e.g. `context_length_exceeded`, `Invalid 'messages'`) in the raised
  error, for both `chat` and `chat_stream`.

## [2.6.1] - 2026-06-04

### Fixes
- **`MCPServer.mount()` returned HTTP 422 for every `/mcp` call.** Under
  `from __future__ import annotations` the route handler's `request: Request`
  annotation is a string that FastAPI resolves against the *module* globals, but
  `Request` was imported locally inside `mount()` — so FastAPI couldn't resolve
  it and treated `request` as a required query param. The MCP server endpoint
  (`initialize`, `tools/list`, `tools/call`) was unusable when mounted. FastAPI
  symbols are now imported at module level (guarded; the `[node]` extra still
  gates actual use). Any agent's tools can now be exposed as a working MCP
  server with `MCPServer(...).mount(app)`.

## [2.6.0] - 2026-06-04

### Streaming + scaffolding
- **`AitherAgent.stream_chat`** — reliable streaming sibling of `chat()`: native
  tool-calling loop (concludes on its own, unlike the text-protocol `stream_react`)
  that emits live `tool`/`tool_result` events and streams the answer as `token`
  events. `token_delay` paces tokens for a typing effect.
- **`adk new <template>`** — scaffold a full, runnable template app. Ships the
  `deep-research` template (a cited web-research agent: pack + server + web UI):
  `adk new deep-research`.

### Fixes
- **BYO-key no longer community-capped.** The free-tier monthly token cap now applies
  only to the metered Aitherium gateway backend, never to a user's own provider key
  (Anthropic/OpenAI/DeepSeek/Ollama/vLLM). Previously it could brick a self-hosted
  agent after ~100k tokens on the user's own key.
- **Stronger, grounded memory recall.** `chat()` now injects the top-6 knowledge-graph
  hits (untruncated) with a hard no-fabrication instruction — was top-3 truncated to
  200 chars, which surfaced partial facts and let weaker models invent the rest.
- **Tool-arg coercion.** `ToolRegistry.execute` coerces LLM-supplied args toward each
  parameter's annotated type, so a tool no longer crashes when a model passes a list
  or string where an int/float/bool was expected.

### Docs
- New `docs/AGENT_DEV_GUIDE.md` — the golden path for building agents/packs.

## [2.5.0] - 2026-06-04

### Continual learning — agents that learn from runs and reuse memory

Agents now improve the second time they do a task instead of starting from scratch.

### Added
- **Recall-before**: `AitherAgent.chat()` searches the local `SkillStore` and injects the
  top matching learned skills as a system block, so the model reuses proven procedures.
- **Learn-after**: `_learn_after()` runs after each response — reinforces (`success_count++`)
  any skills that were recalled+reused, and extracts + saves a NEW skill from successful
  multi-tool runs via `SkillExtractor`. Wired at both `chat()` return paths.
- Gated by `AITHER_SKILLS` (default on). The previously-unused `SkillStore`/`SkillExtractor`
  are now connected to the run loop.

### Notes
- Pairs with the platform-side LearnedProcedure substrate (recipes → scored procedures →
  skills/tools/A2A/packs → MCTS priors → evolution). The SDK shares memory via the Spirit
  bridge when configured.

## [2.0.0] - 2026-06-02

### Open-core boundary (BREAKING)

This release draws a single, enforced free/paid line. The free tier stays
genuinely useful (real agent, typed + graph + code memory, ReAct, ~essential
tools); paid tiers unlock the *scale* capabilities via api.aitherium.com.

### Added
- **`adk/licensing.py`** — the entitlement keystone. Resolves a tier
  (COMMUNITY by default), verifies optional portal-signed `~/.aither/license.json`
  (Ed25519), and answers `can_use_fleet/channels/cron/swarm/auto_neurons`,
  `is_agent_licensed`, `max_effort`, etc. **Fail-closed**: no/invalid/expired
  license → free tier; an unsigned license never grants premium.
  `AITHER_TENANT_SLUG=aitherium` → unrestricted INTERNAL tier.
- **Moat guard** (`scripts/check_moat_boundary.py`) wired into the publish
  workflow — blocks any wheel that leaks the moat or drops the keystone.
- Tests: `tests/test_licensing.py`, `tests/test_moat_boundary.py`.

### Changed (gating — fail-closed)
- Free tier effort is capped at 3 (reasoning effort 7–10 needs Professional).
- Monthly token budget hard-block on the free tier (no silent overspend).
- Gated behind a paid tier: fleet mode (`FleetConfig`), cross-agent delegation
  (`AgentForge.delegate`), channel adapters, cron scheduler, proactive
  auto-neurons, and `swarm`/`swarm_code` dispatch.

### Removed
- **`adk/nanogpt.py`** (on-device training IP) removed from the published SDK
  and the public repo; relocated to the internal AitherOS moat. Excluded from
  the wheel as defense-in-depth.
- Pre-2.0 releases shipped this IP and ungated capabilities and are being
  yanked (PyPI) / removed (GitHub releases). See `scripts/yank_leaky_releases.py`
  and `scripts/purge_public_leaks.sh`.

## [1.20.0] - 2026-05-27

### Fixed
- **Pack discovery path mismatch** — `_find_local_packs()` now scans both
  `~/.aither/packs/` and `~/.aitheros/packs/`, so packs installed via
  `adk pack install` are discovered by `adk-workspace`.

### Added
- **Auto-register on pack install** — `adk pack install` now auto-registers
  with the portal if the pack contains an `agent.yaml` with a `portal` section.
  One command does download + extract + register.
- **Flux event on agent registration** — `developer_portal.py` emits
  `agent.registered` Flux event so the portal fleet UI updates in real-time.

## [1.15.0] - 2026-05-26

### Added
- **Cloud memory** — `adk quickstart --cloud` now tests gateway memory and
  auto-configures Spirit URL so memories persist across devices via
  `gateway.aitherium.com/v1/memory/teach` and `/v1/memory/recall`.
- **Config→env export for cloud mode** — `Config.from_env()` now exports
  `AITHER_CLOUD_MODE`, `AITHER_SPIRIT_URL`, and spirit path env vars from
  saved config so Memory and other modules pick them up without needing
  explicit env vars set by the user.
- **Cloud Quick Start section** in README — 3-command setup for users who
  just want a Claude/GPT-4/DeepSeek API key and the full agent harness.
- **`--memory` profile for node deployment** — `adk deploy node --memory`
  adds Spirit (8087) + WorkingMemory (8101) containers for persistent
  vector memory. Without this, agents use local SQLite only.
- **Configurable vLLM in node compose** — `AITHER_VLLM_MODEL`,
  `AITHER_VLLM_GPU_UTIL`, `AITHER_VLLM_CTX_LEN`, `AITHER_VLLM_QUANT_ARGS`
  env vars let `adk setup` write GPU-detected tier settings to `.env`
  and have the node compose pick them up (TQ4 for small GPUs, BNB for large).

### Fixed
- **Memory cloud routing** — `Memory._spirit_teach()` and `_spirit_recall()`
  now use configurable paths and auth headers for gateway proxy. Previously
  cloud-only users silently lost all Spirit memory calls.

## [1.9.0] - 2026-05-24

### Added
- **Composable ToolPack system** — agents can load tool packs from
  `.toolpack.yaml` directories. Drop a pack in `~/.aitheros/packs/` or
  set `AITHER_PACKS_DIR`, and any agent discovers it automatically.
- **`tools.packs` in agent.yaml** — list tool pack IDs to auto-load
  (e.g. `packs: [crm-tools, calendar-pro]`).
- **`register_tool_packs(agent, pack_ids, packs_dir)`** in `builtin_tools` —
  programmatic pack loading for agents.
- **`AITHER_TOOL_PACKS` env var** — comma-separated pack IDs loaded on
  agent init (alternative to YAML config).
- **Unknown builtin categories resolve as pack IDs** — `tools.builtin:
  [file_io, crm-tools]` now works: `crm-tools` is looked up as a pack.

## [1.8.0] - 2026-05-24

### Removed
- **11 dead modules** (3,023 lines) — `agent_deploy`, `cloud_deploy`, `create`,
  `phonehome`, `sdk_bridge`, `sovereign`, `telemetry`, `telemetry_config`,
  `error_reporter`, `anonymizer`, `package`. None were imported anywhere.
- **29 ghost exports** from `__init__.py` — `secrets`, `otel`, `fs_sandbox`,
  and `vector_memory` modules were listed in `__all__` and `__getattr__` but
  the backing files never existed. Accessing them raised `ModuleNotFoundError`.
- **Stale dist/ artifacts** — removed old wheels/tarballs (v0.4.0–v1.4.1, 11 MB).
- **Orphan test scripts** — `test_phase1.py` and `test_phase23.py` (one-off
  validation scripts not part of the pytest suite).

### Fixed
- **Version mismatch** — `__init__.__version__` now matches `pyproject.toml`
  (was stuck at 1.6.0).

## [1.5.0] - 2026-05-23

### Added
- **`--sovereign` flag for `adk deploy node`** — registers the node with the
  Aitherium hub via federation after deployment. Saves federation credentials
  to `~/.aither/.env.federation`. Accepts `--hub` (custom hub URL) and
  `--tenant` (tenant slug) options. Non-fatal: node runs standalone if
  registration fails.
- **Federation → fleet cross-post** (Genesis) — sovereign nodes that register
  or heartbeat via `/federation/register` and `/federation/heartbeat` now
  automatically appear in the fleet dashboard (`/fleet/deployments`) with
  `node_type: "sovereign"` and live status/metrics.
- **"Deploy Sovereign Node" section on Connect page** (AitherVeil) — Section 8
  with ADK deploy commands, fleet dashboard links, and provision page links.
- **`deploy-node` action in tunnel proxy** (AitherVeil) — Connect page and
  external callers can now request bootstrap scripts via
  `POST /api/tunnel/devworkspace?action=deploy-node`.

### Fixed
- **Fleet provision page** — API URL corrected from `/api/fleet/provision` to
  `/api/bridge/genesis/fleet/provision` (matches bridge proxy pattern).
- **Fleet detail page** — API URL corrected from `/api/fleet/deployments/` to
  `/api/bridge/genesis/fleet/deployments/` (same fix).
- **Dockerfile** — entry point corrected from `aither-serve` to `adk-serve`
  (matches `pyproject.toml` script registration). Version label updated.

## [1.4.1] - 2026-05-22

### Fixed
- **macOS executable builds** — dropped `--target-arch universal2` from
  `packaging/build_executable.py`. PyPI wheels for `pydantic_core` and other
  native deps are arch-specific, not fat binaries, so universal2 always failed
  with `IncompatibleBinaryArchError`. Now builds for the runner's native arch
  (`aither-macos-arm64`).
- **Release workflow strategy** — added `fail-fast: false` so a single
  executable build failure no longer cancels the other OS jobs.
- **PyPI publish** — added `skip-existing: true` so re-runs of the same
  version don't fail when the artifact already exists.
- **GitHub Release job** — now runs whenever `pypi` succeeds even if some
  executable matrix entries fail, so the release page is always created with
  whatever binaries did build successfully.

## [1.4.0] - 2026-05-22

### Added
- **Sovereign template now ships with `agent_core` 5-tier memory** — generated
  backends include the canonical `agent_core/` package (working → episodic →
  semantic → procedural → identity) plus `agent_memory.py` helper, vendored
  from `portal-kit-backend`.
- `aiosqlite>=0.20.0` added to sovereign template `requirements.txt` for
  local agent_core SQLite storage.
- Document tombstoning: `evict_cached_conversations_referencing` is wired
  through document-delete handlers in consumer apps so memories citing
  deleted documents are blocked from recall.
- New tests: 13 smoke tests for `portal-kit-backend/agent_core` covering
  promotion thresholds, decay rates, store roundtrip, tombstone recall
  blocking, circuit breaker state machine, and chat-turn recording.

### Changed
- `scripts/vendor_agent_core.py` now syncs canonical `agent_core` to 4
  targets: WorkspaceRuntime, ADK sovereign template, and the GargBot +
  Chelle consumer mirrors (previously only 2).

## [1.2.1] - 2026-05-21

### Added
- `adk train` command group — 6 subcommands: status, launch, logs, cancel, runs, register-gpu
- `/slash-commands` endpoint — ADK server auto-exposes all 35 CLI commands as structured JSON manifest for AitherShell
- `/cli/execute` endpoint — AitherShell can run any CLI command through the server
- `build_command_manifest()` — introspects full argparse tree (commands, args, choices, subcommands)
- `_register_commands()` extracted from main() for shared parser construction

### Changed
- `_get_genesis_url()` now probes: Genesis → ADK server → Aitherium cloud gateway (works without Genesis)

## [1.2.0] - 2026-05-21

### Added
- **Runtime backend switching** — `agent.switch_backend("deepseek")` and `agent.llm.switch_backend()` change provider without recreating agent
- **Hybrid reasoning** — `agent.set_reasoning_backend("anthropic")` routes effort 7+ to cloud API while keeping local orchestrator
- **First-class DeepSeek provider** — `deepseek` in effort model table with `deepseek-chat` and `deepseek-reasoner`
- **TQ4 quantization tiers** — `nano` (6GB, TurboQuant 4-bit), `standard-tq4` (12GB, both models TQ4), `hybrid-tq4` (6GB + cloud reasoning)
- **`adk backend` CLI** — `list`, `set`, `set-reasoning`, `test` subcommands
- **`adk quickstart`** — unified first-run wizard (setup + auth + shell)
- **`adk tools`** — list local + MCP tools with tier markers
- **`adk backup`** — tarball export of `~/.aither/`
- **`adk ingest`** — feed docs/files into knowledge graph
- **`adk setup nemotron`** shortcut — alias for `--tier lite`
- **`--reasoning-api`** flag in setup — `anthropic`, `openai`, `deepseek`, `gateway`
- **`--dgx-spark URL`** flag — remote vLLM endpoint configuration
- **DGX Spark auto-detection** — scans `spark.local`, `192.168.0.33`, `AITHER_DGX_URL` env
- **Post-setup smoke test** — sends real inference request after container startup
- **Schema migration system** — version table + migration runner in `memory.py` and `graph_memory.py`
- **Configurable vLLM port scan** — `AITHER_VLLM_PORTS` env var for custom ports
- **`aithershell` entry point** — alias for `adk` in pyproject.toml
- Shell auto-downloads binary on first `adk shell` (no `--install` needed)
- Shell pre-flight check warns if no backend detected
- Shell passes full config via env vars (API key, tenant, inference URL)
- DGX/Remote and Cloud API checks added to `adk doctor`
- Config fields: `reasoning_backend`, `reasoning_api_key`, `deepseek_api_key`, `dgx_url`, `vllm_extra_ports`

### Fixed
- `test_aeon_chat_basic` timeout — mock scope was exiting before request
- `adk doctor` check_disk timeout — cap rglob at 5000 files

## [1.1.7] - 2026-05-19

### Added
- Remote agent pipeline — `adk onboard` → fleet dispatch → remote inference
- Agent-to-agent federation with mesh relay
- `adk connect` for desktop mesh enrollment

## [1.1.6] - 2026-05-19

### Fixed
- Tool call execution recovery across all backends
- Hermes tool call fallback parsing for vLLM

## [1.1.5] - 2026-05-19

### Fixed
- Tool call execution + Hermes fallback parsing improvements

## [1.1.0] - 2026-05-18

### Added
- Multi-channel gateway (Telegram, Discord, Slack, webhook)
- Aeon group chat with 7 presets
- Skills auto-extraction from multi-step sessions
- MCP stdio server for Claude Code integration
- `adk gateway`, `adk cron`, `adk skills`, `adk soul` commands
- SOUL.md import/export for portable agent identity
- Elysium cloud device-flow authentication

## [1.0.0] - 2026-05-18

### Breaking
- Package consolidated — `aither_adk` namespace replaced by `adk` + `adk.platform`
- 93 duplicate files removed from `AitherOS/packages/aither_adk/`

### Added
- Standalone core primitives (`adk.core`: Agent, Tool, Memory, ModelBackend, Capability, Trace)
- `adk.platform` sub-package merging all platform toolkit modules
- Compatibility shims for legacy `from aither_adk.*` imports

### Changed
- Version synced across pyproject.toml, `adk/__init__.py`, `adk/platform/__init__.py`, npm package
- Classifier promoted from Alpha to Beta

## [0.16.0] - 2026-04-16

### Swarm Coding Engine & Repowise Integration
- **`repowise_search` tool** — Semantic + keyword hybrid code search via Repowise, with ripgrep fallback
- **`swarm_code` tool** — Dispatch complex tasks to 11-agent swarm pipeline (ARCHITECT->SWARM->REVIEW->JUDGE)
- **`agent.swarm()`** — Async convenience method with configurable mode, effort, timeout
- **`agent.code_search()`** — Async convenience method returning structured results
- New tool categories: `repowise` and `swarm` in TOOL_CATEGORIES
- `repowise_search` and `swarm_code` added to `__init__.py` exports with lazy loading
- `IDENTITY_DEFAULTS` updated: repowise for code-focused agents, swarm for orchestration agents
- Standalone graceful degradation: repowise falls back to ripgrep, swarm returns structured error

## [0.13.0] - 2026-04-02

### Graph Faculties — Local Knowledge for Every Agent
- **CodeGraph** (2,799 lines) — Full Python AST indexer with call graph, keyword/semantic/hybrid query, embedding matrix cache, incremental re-indexing, multi-hop chain expansion
- **MemoryGraph** (1,339 lines) — Graph-based persistent agent memory with 10 edge types, hybrid query (keyword + semantic + graph expansion), multi-hop recall, pickle persistence
- **EmbeddingProvider** — 4-backend fallback chain: sentence-transformers (GPU/CPU) -> Ollama -> Elysium cloud -> feature hashing (zero deps)
- **BaseFacultyGraph** — Abstract base with HMAC-SHA256 validated pickle persistence

### Agent Integration
- `agent.set_code_graph(cg)` — auto-registers `code_search` + `code_context` tools
- `agent.set_memory_graph(mg)` — auto-registers `remember` + `recall` + `memory_stats` tools
- Both graphs inject context into LLM prompts automatically during chat

### Zero-Config Onboarding
- `adk start` / `adk` (no args) — auto-detect project, index code, detect LLM, persistent memory, interactive chat
- Works for any directory: Python codebases, doc folders, mixed workspaces
- Auto-detects LLM: Ollama -> vLLM -> Elysium -> OpenAI -> Anthropic
- Per-project persistent memory in `~/.aither/memory/<project>`
- `adk index <path>` — standalone indexing with progress bar and stats

### MCP Gateway
- `POST /v1/embeddings` — OpenAI-compatible embedding proxy to vLLM-embeddings:8209
- Available to all tiers (embeddings are free)
- ADK EmbeddingProvider uses this as Elysium cloud fallback

### Optional Dependencies
- `pip install aither-adk[graphs]` — numpy for 10x cosine similarity speedup
- `pip install aither-adk[embedding]` — sentence-transformers + torch for local GPU embeddings
- `pip install aither-adk` alone — graphs work with feature hashing (zero deps)

## [0.12.0] - 2026-04-01

### Bootstrap & Service Discovery
- Version handshake -- ADK checks major.minor compatibility with Genesis/Node
- Background reconnect loop -- ServiceBridge re-probes every 30s in standalone mode
- Port 8080/8090 documented -- MCP vs OpenAI-compat clearly separated
- Genesis URL configurable via GENESIS_URL env var (was hardcoded)
- Standalone mode warning -- visible stderr alert when AitherOS not detected
- Auto-reconnect on startup when services come online

### Elysium Cloud Inference
- Unified gateway URL -- gateway.aitherium.com handles auth + billing + inference
- Streaming inference -- SSE passthrough for /v1/chat/completions with stream=true
- Auth proxy routes -- /v1/auth/register, /v1/auth/login, /v1/auth/me
- Billing proxy -- /v1/billing/balance through gateway
- Awconnect Elysium fallback -- cloud inference when local Genesis is down
- AitherDesktop Elysium fallback -- third-tier chat fallback after Node

### Infrastructure
- /discovery endpoint on Genesis -- unified service URLs/versions/health
- /api/config/services on Veil -- runtime port config for client-side JS
- Veil healthcheck gates on Genesis -- unhealthy when backend is down
- Desktop crash detection 90s->60s (threshold 3->2)

## [0.11.0] - 2026-04-01

### Agent Execution Quality (Claude Code Parity)
- Raise loop guard block threshold from 3 to 4 -- agents get more room for iterative search
- Soft synthesis nudge for effort >= 4 (no tool stripping, trust the model)
- max_output_tokens escalation -- retry up to 3x when response is truncated
- Tool result pairing guarantee -- synthetic error for orphaned tool_use blocks
- Micro-compaction of old tool results -- save context tokens on long sessions
- First-turn tool forcing only for effort >= 6 (trust model for lower effort)
- Diminishing returns detection -- nudge agent when 3+ turns produce < 500 tokens
- Message normalization -- merge consecutive same-role messages, strip empties
- LLM retry with exponential backoff (5 retries, 500ms-16s, jitter)

## [0.9.0] - 2026-03-16

The "connected world" release. Cross-platform identity pairing, voice capabilities, and multi-channel integration.

### Added
- **Pairing**: Cross-platform identity linking (`adk/pairing.py`)
  - `PairingManager` — SQLite-backed identity linking with 6-char pairing codes
  - Link users across Telegram, Discord, Slack, WhatsApp with 10min TTL codes
  - Canonical session IDs for cross-channel conversation continuity
  - `get_session_id()` returns "user-{id}" for paired users
- **Voice**: Speech-to-text and text-to-speech client (`adk/voice.py`)
  - `VoiceClient` — async STT/TTS/emotion via AitherVoice service
  - Convenience functions: `hear()`, `say()`, `feel()`
  - 6 voice options: alloy, echo, fable, nova, onyx, shimmer
  - Emotion detection with intensity scoring
- New exports: `PairingManager`, `PairingResult`, `PlatformIdentity`, `VoiceClient`, `TranscriptionResult`, `SynthesisResult`, `EmotionResult`

### Changed
- `__init__.py` exports expanded with pairing and voice symbols

## [0.6.0] - 2026-03-13

The "group mind" release. Multi-agent group chat, creative tools, and Iris identity.

### Added
- **Aeon**: Multi-agent group chat engine (`adk/aeon.py`)
  - `AeonSession` — persistent group chat with parallel agent execution
  - 7 presets: balanced, creative, technical, security, minimal, duo_code, research
  - Orchestrator synthesis: Aither summarizes all agent responses
  - Serial execution for Ollama, parallel for vLLM/cloud
  - ConversationStore persistence with `type: "aeon"` metadata
  - `group_chat()` one-shot convenience function
- `aither aeon` CLI command — interactive terminal group chat with color-coded agents
  - `-p/--preset`, `-a/--agents`, `-r/--rounds`, `--no-synthesize` flags
  - `reset` and `quit` commands
- Server endpoints: `POST /aeon/chat`, `GET /aeon/presets`, `GET /aeon/sessions/{id}`
- Creative tools in builtin_tools: `image_generate`, `image_refine`, `image_search`, `video_generate`
- Iris agent identity with visual generation capabilities
- 48 Aeon tests (data models, presets, chat, context, persistence, server, exports)

### Changed
- `__init__.py` exports: `AeonSession`, `AeonResponse`, `AeonMessage`, `group_chat`, `AEON_PRESETS`

## [0.5.0] - 2026-03-13

The "tenant-ready" release. Multi-tenant admin, permission grants, safety profiles, and a full setup wizard.

### Added
- `aither setup` interactive setup wizard with hardware detection, model selection, identity config
- Strata storage backend: SQLite WAL persistence for conversations, memories, knowledge graphs
- CLI test runner: `aither test` with auto-discovery and parallel execution
- Permission grants system for MCP tool access control
- MCP account management tools
- LLM provider auto-detection for Ollama and vLLM endpoints
- Apache-2.0 LICENSE file
- Elysium desktop sync module
- Comprehensive CLI test suite (493 tests)
- Strata storage test suite (972 tests)
- LLM provider tests (82 tests)

### Changed
- CLI expanded: `aither setup`, `aither test`, `aither bugreport`, `aither doctor`
- Strata module rewritten as full local-first storage engine
- Server startup includes Strata initialization
- README rewritten with clearer quickstart and architecture docs

### Fixed
- Dead documentation links
- Python 3.11 compatibility issues
- Ruff per-file-ignores configuration
- Identity provisioning edge cases
- Docker node-gyp native dependency builds (python3 + build tools)

## [0.4.0] - 2026-03-13

The "own your AI" release. Self-hosted agent OS for people who don't want their data on someone else's servers.

### Added
- Muse agent identity (creative/artistic generation)
- Port 8120 to vLLM scan for ExoNodes discovery
- Public roadmap with milestone-based porting schedule
- Competitive positioning: self-hosted alternative to cloud-locked AI appliances

### Changed
- LLM router: compute-aware effort routing (replaces effort-level context gating)
- README rewritten for sovereignty-first messaging
- Package description updated
- Promoted from alpha to stable release

### Fixed
- Version string consistency between pyproject.toml and __init__.py
- vLLM port scanning now includes port 8120 (ExoNodes)

## [0.3.1] - 2026-03-09

### Added
- GraphMemory: SQLite knowledge graph with Ollama embeddings and hybrid search
- NeuronPool: auto-fire pre-LLM data gathering agents
- NanoGPT: pure Python char-level transformer with LoRA fine-tuning
- Safety gates: IntakeGuard (input), LoopGuard (recursion), Sandbox (code exec)
- Event system: EventEmitter with chat/tool/forge event types
- Builtin tools: identity-based tool selection
- ServiceBridge: auto-discovery of AitherOS services
- Streaming: chat_stream with safety gate integration
- Auth middleware: Bearer token authentication
- CLI: `aither init` and `aither serve` commands

### Changed
- Wired 5 previously disconnected modules into agent loop
- 522 total passing tests (up from 85)

## [0.3.0] - 2026-03-07

### Added
- Clean-room agent development kit
- Multi-backend LLM providers (Ollama, OpenAI, Anthropic)
- AitherAgent class with @tool decorator
- SQLite conversation memory
- OpenAI-compatible server (`aither-serve`)
- 16 agent identities as package data
- MCP bridge to mcp.aitherium.com
- Privacy-centric opt-in telemetry
- Bug reporting CLI and API
- FastAPI server with OpenAI-compatible endpoints
- Hardware auto-detection (5 tiers, 11 profiles)
- Fleet orchestration and multi-agent coordination
- A2A mesh protocol
- Federation protocol for cross-instance agent dispatch

### Initial release
- 85 passing tests
- Apache-2.0 license

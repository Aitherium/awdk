# Aither — ACP + A2A integration for JetBrains

Drives AitherOS agents from any IntelliJ Platform IDE (IntelliJ IDEA, PyCharm,
WebStorm, …) over the **Agent Client Protocol (ACP v2)** and **A2A**.

## What it does

| Surface | How |
|---|---|
| **ACP (chat with an agent)** | Spawns `adk acp serve --name <agent>` (the awdk ACP server, stdio JSON-RPC v2) and drives the full session lifecycle: `initialize`, `session/new`, `session/prompt`, `session/resume` (the human-in-the-loop approval gate), `session/cancel`, `session/close`, `session/list`. |
| **A2A (message a remote agent)** | Sends a signed `message/send` over HTTP to a remote agent's `/a2a` endpoint, using the caller agent's Ed25519 keypair (`~/.aither/agent_key.<name>.pem`) — the same wire awdk's Python `a2a_client` produces, so the mesh trusts it. |

Requires the `awdk` CLI on PATH (`adk acp serve`).

## Build

```bash
cd editors/jetbrains-aither
gradle wrapper --gradle-version 8.9     # first time only (no system gradle)
./gradlew build                         # downloads the IntelliJ SDK, compiles, packages build/libs/*.zip
./gradlew runIde                        # launch a sandbox IDE with the plugin
```

Install the built `build/libs/*.zip` via *Settings → Plugins → ⚙ → Install Plugin from Disk*.

## Use

1. **Tools → Open Aither Agent** (or the *Aither Agent* tool window).
2. Type an agent name (`atlas`, `lyra`, `demiurge`, …) → **Connect**. The plugin
   spawns `adk acp serve` and creates a session.
3. Chat in the input box. If the agent needs approval for a tool call
   (`session/request_permission`), the panel asks — reply `y`/`n`.
4. Switch to **A2A** mode and give a target (a mesh agent name or a raw
   `http://host:port`) to message that agent directly.

## Verified

- `acp/` + `a2a/` clients compile standalone (kotlinc 2.0.21, org.json,
  kotlinx-coroutines).
- Live round-trip against the real `adk acp serve` (2026-08-08): `initialize`
  → `session/new` → `session/list` all succeed; server log lines interleaved in
  the stdio stream are skipped, not misread as protocol errors.
- `Ed25519Public` (RFC 8032 public derivation) matches the canonical test
  vector and awdk's cryptography-based derivation.

The IntelliJ UI shell (`ui/`, `OpenAitherAction`) compiles against the IntelliJ
Platform SDK — run the Gradle build above to compile it.

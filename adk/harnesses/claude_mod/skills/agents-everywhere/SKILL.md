---
name: agents-everywhere
description: Turn an agent idea into a verified, shareable Aitherium tool across awdk, awsh, MCP, WebMCP, PWA, Codex, Claude Code, and other agent CLIs. Use when bootstrapping local inference, connecting existing agents, or packaging a reusable agent tool.
---

# agents-everywhere — idea to a tool another agent can actually use

Use this skill when the goal is larger than installing one package: a local model,
agent tools, identity, MCP, browser/PWA access, and a handoff another person can run.
Compose the existing skills in this repository; do not replace their hardware sizing,
provider, or agent-specific instructions.

## The invariant

Build one capability surface and give it several clients:

```text
Codex / Claude Code / Aider / Cursor / Cline / OpenClaw / other CLI
                         │ MCP or model-provider protocol
          awnboard → awiam → awbac → awdit → awtunnel
                         │ approved capability
       awsh / awdk → awm · awrepl · awgraph · awgit · awnode
                         │
                  local llama.cpp / Bonsai
```

The browser/PWA is a thin control surface. It must not contain model-provider keys,
workspace credentials, or an unauthenticated MCP endpoint.

## 1. Discover before installing

Run read-only checks first and record the results:

```bash
python --version
adk --version 2>/dev/null || true
awsh --version 2>/dev/null || true
claude --version 2>/dev/null || true
codex --version 2>/dev/null || true
ollama --version 2>/dev/null || true
nvidia-smi --query-gpu=name,memory.total --format=csv 2>/dev/null || true
```

Read `aither-start` for hardware/model selection, `local-inference` for a real local
round-trip, and `awdk` for runtime setup. Never call a model “local” until a real
request has completed against the local endpoint.

## 2. Bootstrap the local Aither World

The universal terminal front door is **AitherShell / `awsh`**. Use the official
one-command installer for a laptop, or the phone-specific installer inside Termux or
the Pixel Linux Terminal:

```bash
curl -fsSL https://aitherium.com/install.sh | sh
curl -fsSL https://aitherium.com/phone.sh | bash
awsh --version
```

Review the installer source or pin a verified release when building a production
bootstrapper. The rest of this skill describes the inspectable recovery path when the
official installer is unavailable or when a project wants to own every step.

On Windows, prefer **AitherZero** as the orchestration layer. It gives a blank
machine a PowerShell-native path through the Aitherium products and keeps every
bootstrap step inspectable (numbered scripts in `library/automation-scripts/`)
instead of hiding them behind a monolithic installer. Nothing is required up
front — not PowerShell 7, not git:

```powershell
# Windows - any PowerShell window (5.1 is fine). Installs pwsh 7, fetches
# AitherZero to ~/.aitherzero, runs the dev-workstation playbook:
# Python, Git, Node LTS, gh, awdk (adk), awsh, then verifies each one.
irm https://raw.githubusercontent.com/Aitherium/AitherZero/main/bootstrap.ps1 | iex
```

```bash
# macOS / Linux / Termux (Termux runs the same steps natively; pwsh cannot run there)
curl -fsSL https://raw.githubusercontent.com/Aitherium/AitherZero/main/bootstrap.sh | sh
```

Every step is idempotent; re-run the same line to repair a partial install.
From an existing checkout the equivalent is `Invoke-AitherPlaybook dev-workstation`
(`-DryRun` to preview). `node-onboard` is a different playbook — it enrolls the
machine as a cluster node and needs an enrollment token. Keep local overrides in
`config/config.local.psd1`; do not put provider keys or device tokens in a
repository. Do not assume `bash -lc` exists on Windows.

After the playbook, verify the actual runtime rather than only the package install:

```powershell
adk status
awsh --version
adk doctor
```

`adk doctor` warnings about optional repository-development variables are not proof
that inference is broken; a local model is only **running** after a real request has
completed against the local endpoint.

Prefer the supported awdk onboarding path, then prove it:

```bash
python -m pip install awdk
adk onboard --quick
adk doctor
```

Add only the bricks the requested capability needs. A typical coding surface is:

```bash
python -m pip install awm awnode awrepl awgraph awgit awnboard
python -m pip install \
  git+https://github.com/Aitherium/awiam.git \
  git+https://github.com/Aitherium/awbac.git \
  git+https://github.com/Aitherium/awdit.git \
  git+https://github.com/Aitherium/awtunnel.git
```

Use the repository's `bonsai-27b` and `local-inference` guidance to select a model that
fits the actual machine. `adk login` and `adk enroll` are optional control-plane steps;
they must remain a visible user decision.

## 3. Put authorization in front of tools

Before exposing a tool to another agent, define the requested capability and enforce it:

| Layer | Job |
|---|---|
| `awnboard` | front gate for local, LAN, and remote requests |
| `awiam` | resolve the caller, device, and session |
| `awbac` | fail-closed role/capability decision |
| `awdit` | append-only decision/tool-call record |
| `awtunnel` | private reachability for a host with no public address |

Start with narrow scopes such as `local.inference`, `mcp.tools`, `workspace.read`,
`memory`, `repl`, and `lan.control`. Do not silently grant `workspace.write`, shell,
or public-network access. If identity, policy, or audit cannot run, stop before the
tool call rather than treating “not configured” as allowed.

## 4. Connect the clients

For a local stdio MCP server, generate a config from the actual Python environment and
project path. Do not paste a guessed path into a shared recipe.

Claude Code project scope:

```bash
claude mcp add forgepilot --scope project -- python -m aitherium_pack mcp
claude mcp get forgepilot
```

Codex CLI:

```bash
codex mcp add forgepilot -- python -m aitherium_pack mcp
codex mcp list
```

For a remote client, use authenticated HTTPS MCP and the client’s own OAuth/device
approval flow. For Claude Code, use `--transport http` and `/mcp` when the server is
remote; for a phone/PWA, never route to a raw loopback or public unauthenticated port.

Use `agent-integrations` for model-provider protocol choices and `install-skills` for
the target agent's skill layout. The same MCP server can be shared by Claude Code,
Codex, Aider, Cursor, Cline, OpenClaw, Goose, Gemini CLI, and other MCP clients, but
each client still needs its own explicit configuration and permission check.

## 5. Switch coding backends without rebuilding the pack

The ADK already exposes the common Claude Code backend profiles. Surface those
commands in your UI/skill instead of hard-coding provider environment variables:

```bash
adk claude-model code                 # DeepSeek Flash fast coding profile
adk claude-model status               # show the active profile
adk claude-model check --timeout 30   # prove a real turn works
```

Other aliases include `plan`, `reason`, `local`, and `fast`; use `adk claude-model
use <profile>` for a named profile. `awsettings` is the portable settings and
approval layer when present, but it is not a guaranteed standalone executable on
every host. A bootstrapper must detect that fact and keep working without it. Never
put provider API keys into an awskill, MCP config, WebMCP bundle, or PWA.

## 6. Add the browser and phone surface

For a WebMCP-capable page, register a tool that delegates to the guarded MCP executor;
the page itself should not implement privileged actions. For a PWA:

1. publish the static shell over HTTPS;
2. configure its MCP endpoint to the authenticated remote service;
3. install it with **Add to Home Screen / Install app**;
4. keep inference and credentials on the host;
5. test from a phone on the same LAN before adding a tunnel.

If the user wants a local-only phone flow, bind the host to a trusted LAN and explain the
boundary. If the user wants internet access, require an authenticated tunnel and TLS.

## 7. Make it shareable

Every new tool should leave behind:

- a short manifest: name, interfaces, requested scopes, entrypoints, and version;
- MCP client config for Codex/Claude or a command that generates it;
- a WebMCP registration stub with no secrets;
- a PWA/static deployment path;
- a README with install, approval, and proof commands;
- a test or live check that fails when the connection is broken;
- an artifact/report with hashes when the result is intended for handoff.

Prefer the existing `awskills` installer for distributing the skill. A skill is not the
runtime: ship the MCP server, scripts, or pack metadata required by the tool as separate,
inspectable artifacts.

## 8. Self-service authoring under `tools/agent/`

When someone wants to make a new tool, scaffold it instead of asking them to understand
every client format first:

```bash
python tools/scaffold-agent-tool.py "My Agent Tool" \
  --description "Does one useful thing for another agent" \
  --capability mcp.tools
```

The generator creates `tools/agent/<slug>/` with a manifest, default-deny policy, `SKILL.md`,
MCP config, WebMCP registration stub, README, and review/publish helpers. The default trust
chain is `awnest` (prove there is a human) → `awnboard` (front door) → `awiam` (caller/session)
→ `awbac` (capability decision) → `awdit` (record). It never creates wildcard or shell access.

The author fills in the executor and tests the same capability through Codex, Claude Code,
Aider, Cursor, Cline, Roo, Goose, Gemini CLI, AitherOS, or a phone/PWA. Use `install-skills`
for target skill layouts and `agent-integrations` for model-provider protocol choices.

Share by committing only the generated directory to a reviewable repository or by publishing
an artifact through the chosen awshare/Git flow. Never put access tokens, private URLs, local
absolute paths, or model-provider keys in `tools/agent/`.

## Done means proven

Report these separately:

1. **Detected** — command/package exists.
2. **Configured** — client config was written or an exact installer was generated.
3. **Authenticated** — the user approved the requested scopes.
4. **Running** — the local model/server is live.
5. **Proven** — a real client invoked a real tool and the expected artifact/result exists.

Never collapse a generated command, a demo/offline fallback, and a successful local
inference round-trip into the same claim.

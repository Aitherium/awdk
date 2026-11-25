---
name: antigravity-awdk
description: Two-way Google Antigravity + awdk/awsh/awnode/AitherOS operating guide. Use when running adk antigravity setup/doctor, spawning or steering Antigravity sessions from awsh, delegating to local models, or configuring Google Managed Agents.
---

# Google Antigravity + awdk / awsh / awnode

This skill ships with the `awdk` Antigravity plugin and covers both directions of the integration.

## 1. Antigravity → awdk / awsh / awnode

- **Setup & audit**:
  ```bash
  adk antigravity setup [--repo /path/to/repo]
  adk antigravity doctor
  ```
- **MCP surfaces**:
  - `awdk-awsh` (`python3 -m adk.harnesses.mcp_stdio`) — drive peer harnesses (`awdk`, `codex`, `opencode`, `gemini`, `aither`, `antigravity`).
  - `awdk-local` (`python3 -S -m adk.awdelegate --mcp`) — delegate research/summarization to local models / local router so frontier context stays lean.
  - `aitherium` (`https://mcp.aitherium.com/mcp`) — Streamable HTTP connector to AitherOS cloud/fleet services.
- **Lifecycle hooks**: `adk.harnesses.antigravity_hooks` bridges Antigravity `SessionStart`, `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, and `Stop` into `hookd` (`:8369/pdp` + `/run`), `awsettings-guard`, `awm`, `awrelay`, `awvoice`, and `~/.aither/steer/<conversationId>/`.
- **Settings & skills sync**:
  - `awsettings --domain antigravity pull|push` syncs portable Antigravity settings while keeping machine-local hostnames and credentials at home.
  - `awskills` installer targets Antigravity directly via `--target antigravity`.

## 2. awdk / awsh / awdesk / awconnect → Antigravity

- **Harness registry**: `antigravity` is registered in `adk.harnesses.registry` (`adk shell new --harness antigravity`).
- **Live session discovery**: every active Antigravity conversation is recorded in `~/.aither/antigravity_sessions/` and surfaced in `GET /sessions/unified`, making it visible in **`awsh`**, **`awdesk`**, **`awconnect`**, and **`awfocus`**.
- **Steering**: drop a message via `awfocus ask <conversation_id> "<text>"` or the `antigravity_steer` toolpack tool; the `PreInvocation` / `Stop` hook drains `~/.aither/steer/<conversation_id>/` into the conversation.
- **Google Managed Agents**: configure BYOK with `adk agent managed byok --provider google` (backed by `GoogleManagedBackend` supporting both `google-antigravity` Python SDK and Gemini API).

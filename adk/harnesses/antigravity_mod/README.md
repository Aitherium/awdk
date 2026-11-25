# awdk for Google Antigravity

First-class two-way plugin connecting **Google Antigravity 2.0 / CLI** with **`awdk`**, **`awsh`**, **`awnode`**, and the **`aw*` brick family**.

## Quick Install

```bash
pip install awdk
adk antigravity setup
adk antigravity doctor
```

Or copy this directory into `~/.gemini/config/plugins/awdk/` (works out of the box once `awdk` is installed on `python3`).

## What's Included

- **MCP Servers** (`mcp_config.json`): `awdk-awsh` (multi-harness plane), `awdk-local` (`awdelegate` local-model offload), and `aitherium` (`https://mcp.aitherium.com/mcp`).
- **Lifecycle Hooks** (`hooks.json`): Bridges `SessionStart`, `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, and `Stop` through `adk.harnesses.antigravity_hooks` to `hookd` / `awgov`, `awsettings-guard`, `awm`, `awrelay`, `awvoice`, and the unified steering mailbox (`~/.aither/steer/`).
- **Subagent** (`agents/aw.md`): Native `aw` subagent for delegating to local models or peer harnesses.
- **Rules & Skill** (`rules/AGENTS.md`, `skills/antigravity-awdk/SKILL.md`): Platform-first operating doctrine and 2-way usage guide.
- **Reverse direction (`awsh` → Antigravity)**: `adk shell new --harness antigravity` plus the `antigravity` toolpack (`antigravity_sessions`, `antigravity_spawn`, `antigravity_steer`, `antigravity_doctor`, `antigravity_setup_status`) and live session discovery for `awdesk`, `awconnect`, and `awfocus`.

---
name: acp-serve
description: >-
  Expose an AitherOS agent to ACP editors. ACP editors — JetBrains IDEs, Zed, VS Code, neovim, Obsidian — drive coding agents over the Agent Client Protocol. adk acp serve makes any AitherOS agent one of those agents: the editor spawns it as a subprocess, and every prompt, permission request and tool round-trip goes over stdio JSON-RPC. Your agent's approval gate maps onto ACP permission requests.
---

# acp-serve — expose an AitherOS agent to ACP editors

ACP editors — JetBrains IDEs, Zed, VS Code, neovim, Obsidian — drive coding
agents over the Agent Client Protocol. `adk acp serve` makes any AitherOS
agent one of those agents: the editor spawns it as a subprocess, and every
prompt, permission request and tool round-trip goes over stdio JSON-RPC. Your
agent's approval gate maps onto ACP permission requests.

## Serve

```bash
adk acp serve                              # serve the default agent on stdio
adk acp serve --name "atlas" --model <backend>   # name + explicit LLM backend
```

The served agent is a real `AitherAgent` on your configured LLM backend. It
fails **loud at startup** if no backend is configured, rather than hanging at
the first prompt.

## Wire up an editor

```bash
adk acp config zed            # zed | jetbrains | vscode | neovim
```

Each emits an ACP `agent.json` whose `runtime.command` runs `adk acp serve`.
Save it where the editor's ACP integration expects it (e.g. Zed:
`.zed/agents/awdk/agent.json`), then restart the editor.

## What the editor gets

- `session/new` / `list` / `resume` / `close` / `delete` — full session surface.
- The prompt lifecycle: running → tool calls → idle with a stop reason.
- `session/request_permission` — your agent's approval gate, rendered as a
  permission card in the editor.
- Streamed agent messages and usage, live.

## Verify

```bash
adk acp prompt --command "adk acp serve" "hello"   # self-drive over stdio
```

A reference ACP client driving `adk acp serve` and completing a running→idle
turn with an approve/deny card is the acceptance test for a new surface.

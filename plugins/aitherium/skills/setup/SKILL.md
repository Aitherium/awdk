---
name: setup
description: Set a person up on Aitherium from Claude Code - check what local model their machine can run, install the awdk agent toolkit and that model with their approval, and sign them in to their Aitherium account. Use when the user asks to install, set up, try or connect Aitherium, AitherOS, awdk or a local Bonsai model.
---

# Set up Aitherium

Goal: the user ends with a working local agent and model on this machine and,
if they want one, a signed-in Aitherium account. Every step that installs or
downloads something needs the user's yes first.

## 1. Say what it is

Call `about_aitherium` on the `aitherium` MCP server. It needs no account. Give
the user the short version: Aitherium is an agent platform they run themselves;
awdk installs an agent and a local model on their machine; the hosted part is
the account and workspace.

## 2. Check what this machine can run

Measure memory with a read-only command, after telling the user you will:

- Windows: `powershell -NoProfile -Command "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB; (Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM)"`
  (`AdapterRAM` caps at 4 GB; if an NVIDIA card is present prefer
  `nvidia-smi --query-gpu=name,memory.total --format=csv`).
- macOS: `sysctl -n hw.memsize` and `uname -m` (`arm64` means unified memory).
- Linux: `free -g` and `nvidia-smi --query-gpu=name,memory.total --format=csv`
  if it exists.

Call `check_local_model_fit` with `ram_gb`, `gpu_vram_gb`, and
`unified_memory: true` on Apple Silicon. Report the recommended model, its
download size, and the rule the tool states. If nothing fits, say so and offer
the hosted option the tool links; do not suggest a model the tool said does not
fit.

## 3. Install, with approval

The installer and its exact one-line command are published at
https://aitherium.com/install. Fetch that page, show the user the command for
their operating system and what it will do, and run it only after they agree.
Do not compose an install command from memory and do not pipe a script from any
other host.

After it finishes, verify instead of assuming: run `adk --version` and
`adk doctor`, and report what they print. If either fails, show the output and
use `search_docs` for the error before guessing.

## 4. Sign in (optional)

Account tools (`account_status`, `recall`, `list_services`,
`search_platform`) need an Aitherium sign-in. Tell the user to run `/mcp`,
choose `aitherium`, and authenticate; the page lets them create a free account.
Then call `account_status` and report the plan and workspace.

## 5. Connect this machine's models to Claude (optional)

When the user wants Claude to use the hardware and local models on their own
machine, hand off to awnode; do not build another path. `about_aitherium`
returns the current steps under `connect_your_own_machine`:

1. `pip install awnode`
2. `awnode setup` (detects the GPU and pulls a model that fits; ask first)
3. `awnode mcp-config` prints the entry for Claude Code or Claude Desktop on
   this machine. Show it and add it only with the user's yes.
4. `awnode doctor` to verify.

This is a local connection on the same machine. Reaching the machine from
claude.ai on the web or a phone is not available yet; say so if asked. Do not
open a public tunnel to the awnode port: `awtunnel up` gives a public URL with
no sign-in in front of it.

## Rules

- The account is whoever signed in. Never ask for, or pass, an account or
  workspace id to a tool.
- Never ask the user to paste a password, API key or token into the chat.
- Say what you measured and what you did not. If a step was skipped, say so.

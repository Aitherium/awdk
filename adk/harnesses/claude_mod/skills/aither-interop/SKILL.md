---
name: aither-interop
description: >-
  Use Aither from Hermes Agent and other harnesses, in both directions. Add awskills as a Hermes skill tap, point Hermes at a local Aither gateway over MCP with `awnode mcp-config --format hermes`, install a Hermes or agentskills.io skill into adk with `adk skills install hermes:<owner>/<repo>/<skill>`, and call another agent over A2A with `adk mesh ls` / `adk invoke`. Includes the trust boundary for third-party packs and the two places each side stores skills.
---
<!-- Generated from the awskills pack (skills/aither-interop.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# aither-interop — Aither and Hermes, both directions

Three things cross the boundary: **skills** (instruction documents), **tools** (MCP), and
**agent-to-agent calls** (A2A). Each has a command on each side; nothing here needs a
plugin, an account, or a hosted service.

## 1. Add awskills as a Hermes skill tap

A tap is a GitHub repo laid out as `skills/<name>/SKILL.md` — which is exactly what
`Aitherium/awskills` publishes.

```bash
hermes skills tap add Aitherium/awskills
hermes skills search interop --source github
hermes skills inspect Aitherium/awskills/aither-interop
hermes skills install Aitherium/awskills/aither-interop
```

The install slug is the directory name. Custom taps are not part of Hermes' centralized
search fallback, so pass `--source github` when searching one. A direct
`owner/repo/path` install works without adding the tap first; adding it is what makes the
pack browsable.

Skills land in `~/.hermes/skills/`. Each installed skill is also a slash command.

**Check:** `hermes skills list --source hub` shows the skill.

## 2. Point Hermes at the local Aither gateway (MCP)

`awnode` is the local gateway. It prints the exact block for Hermes' config, including
the absolute path to its own executable on this machine:

```bash
awnode mcp-config --format hermes
```

That prints an `mcp_servers:` mapping. Paste it into `~/.hermes/config.yaml` (merge it
with any existing `mcp_servers:`, do not add a second top-level key), then test the
connection:

```bash
hermes mcp test awnode
```

`awnode mcp-config` with no `--format` prints a `.mcp.json` block for Claude Code and
other MCP clients instead — same server, different envelope.

**Check:** `hermes mcp test awnode` connects and lists the server's tools.

## 3. Install a Hermes / agentskills.io skill into adk

adk reads the same two sources Hermes does, and fetches exactly one `SKILL.md`:

```bash
adk skills search "deploy"                                  # the awskills shelf first
adk skills install hermes:<owner>/<repo>/<skill> --allow-community
adk skills install agentskills:<owner>/<repo>/<path> --allow-community
adk skills install hermes:<owner>/<repo>/<skill> --allow-community --target project
```

`hermes:` resolves to `<repo>/skills/<skill>/SKILL.md`; `agentskills:` resolves to
`<path>/SKILL.md`. `--target` picks where it is written: `claude` = `~/.claude/skills`
(Claude Code and awsh), `project` = `./.claude/skills`, `adk` = `~/.aither/skills`
(the default). Only `SKILL.md` is fetched — a skill's `references/` and `scripts/` are
not, so read the source if the skill depends on them.

Any owner other than Aitherium is **community trust**: the CLI refuses without
`--allow-community`, and the agent-side `install_pack` tool asks the owner before a
third-party install. Read the skill before you allow it — it is instructions the model
will follow.

**Check:** `adk skills list` shows it under the target you chose.

## 4. Take an adk skill into another harness

```bash
adk skills install aither-interop --target project   # writes ./.claude/skills
adk skills export                                    # every skill as agentskills.io JSON on stdout
```

The exchange format is `skills/<name>/SKILL.md` with `name` matching the directory and a
description of at most 1024 characters. Any tool that reads the agentskills.io layout
reads the exported tree without conversion.

## 5. Agent to agent (A2A)

adk agents advertise their tools as A2A services on your mesh. Two read commands and one
call:

```bash
adk login                    # device-flow; you approve in the browser
adk mesh join                # put this device on your mesh
adk agents ls                # every agent in the mesh and its inference backend
adk mesh ls --format json    # peer agents and the A2A services each advertises
adk invoke <agent> <skill> --arg k=v --arg j='{"n": 2}'
```

`adk invoke` signs as your agent keypair; `--as-agent <name>` selects another
(`~/.aither/agent_key.<name>.pem`). `--arg` values are JSON-parsed when possible, so
booleans and numbers survive the trip. `adk relay join` is the other direction: this
agent joins AitherRelay and answers DMs on its own inference.

**Check:** `adk mesh ls` names the peer and the skill you intend to call, and
`adk invoke` returns that skill's result.

## Trust boundaries, in one place

| what | who reviews it | how it is gated |
|---|---|---|
| awskills skills | Aitherium (MIT) | published from the monorepo, boundary-scanned in public CI |
| a Hermes tap / agentskills.io skill | that repository's owner | `--allow-community` for anything not owned by Aitherium |
| a community pack | nobody at Aitherium | `--allow-community`, and the agent asks the owner first |
| an MCP server you add | you | the server's own tool list; Hermes marks a portable server `untrusted` when the package asks it to |

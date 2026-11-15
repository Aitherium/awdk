---
name: aither-awdk-quickstart
description: >-
  Install awdk and run an AI agent on your own machine, then find, inspect and install packs and skills with the adk CLI. Covers `adk onboard --quick`, `adk doctor`, the pack verbs (list, search, info, install, enable), the skills verbs (list, search, install, export), `adk tools list`, and running an agent locally with `adk run`. Every command here was run against the installed awdk CLI; flags that are not in each command's `--help` are not in this skill.
---
<!-- Generated from the awskills pack (skills/aither-awdk-quickstart.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# aither-awdk-quickstart — install awdk, run an agent, install a pack

[awdk](https://github.com/Aitherium/awdk) (MIT, on PyPI as `awdk`) is a Python agent
runtime: the agent loop, a tool registry, packs of tools, a skills installer and a CLI
called `adk`. The model can be local (llama.cpp, vLLM, Bonsai) or any hosted provider you
already have a key for.

**Check:** `adk --version` prints a version.

## 1. Install

```bash
pip install awdk
adk --version
```

`adk --help` lists the subcommand surface. Python 3.10 or newer.

## 2. Bring up inference and verify the install

```bash
adk quickstart-local --dry-run   # print what it would detect and pull; changes nothing
adk quickstart-local             # detect CPU/RAM/GPU, pick a backend, pull and serve a model
adk doctor                       # toolkit, packs, inference, enrollment — one report
```

`adk quickstart-local` writes its endpoint on a local port (default `8209`). Backends:
`--backend auto|llamacpp|vllm|bonsai`. To use a hosted provider instead, set a key — the
CLI prompts, so the key never lands in your shell history:

```bash
adk keys set anthropic     # or openai / deepseek / openrouter / groq / together / google
adk models                 # what this install can actually reach
```

One-shot setup instead of the two steps above:

```bash
adk onboard --quick        # detect hardware, run inference, install the default pack, enroll
adk onboard --quick --pack hermes
```

**Check:** `adk doctor` reports inference reachable and at least one pack installed.

## 3. Find and install packs

A pack is a bundle of tools an agent can call. Four verbs cover the whole lifecycle:

```bash
adk pack list                      # installed + available
adk pack search "pdf"              # search by name, description or tags; no query = everything
adk pack info <pack-id>            # what it installs, its tools, its trust tier
adk pack install <pack-id>         # a bare id resolves across the pack shelves
adk pack enable <pack-id>          # turn it on for this agent (installs first if missing)
adk pack update <pack-id>          # or with no id: every installed pack
adk pack remove <pack-id>
```

A bare id resolves in order: first-party agent packs, marketplace/library packs, then
gateway extensions. Pin the shelf explicitly when a name is ambiguous:
`awpack:<id>`, `library:<id>`, `ext:<id>`, `skill:<name>`. A community pack nobody at
Aitherium reviewed refuses unless you pass `--allow-community` — read it first.

`adk pack install` also takes a local bundle: `adk pack install ./dist/mypack-0.1.0.tar.gz`.
The community marketplace path is `community:<listing-id>`.

**Check:** `adk pack info <pack-id>` shows it, and `adk tools list` lists its tools.

## 4. Find and install skills

Skills are instruction documents (`SKILL.md`), not code. The built-in shelf is the
public awskills pack; `install` copies one into a harness skills directory.

```bash
adk skills list                                  # learned + installed
adk skills search "git"                          # learned skills + the awskills shelf
adk skills install acp-drive --target claude     # ~/.claude/skills (Claude Code and awsh)
adk skills install aither-interop --target project   # ./.claude/skills, this repo only
adk skills export                                # every skill as agentskills.io JSON on stdout
adk skills update                                # re-install what is already installed
adk skills remove <name>
```

Third-party skills come from the two places Hermes Agent reads, fetched as a single
`SKILL.md`: `hermes:<owner>/<repo>/<skill>` (a tap, `<repo>/skills/<skill>/SKILL.md`) and
`agentskills:<owner>/<repo>/<path>`. Both are community trust unless the owner is
Aitherium, so they need `--allow-community`.

**Check:** `adk skills list` shows the skill under the target you chose.

## 5. Run an agent locally

```bash
adk install pack:openclaw        # or pack:hermes / pack:claude-code
adk run --agents openclaw        # start the agent server
adk agents ls                    # agent names you can talk to
adk chat <agent>                 # talk to a running agent by name
```

`adk run` flags that matter: `-m/--model`, `-b/--backend`, `-p/--port`, `-a/--agents`
(comma-separated identities), `--mesh` (advertise tools and inference to connected
peers). To run a persistent agent tied to a fleet, use `adk up` instead.

**Check:** `adk run` stays up and `adk chat <agent>` answers a prompt.

## Exit codes

Every `adk` verb exits `0` clean, `1` refused or invalid, `2` could not judge. In a
script, treat `2` as a failure — it is not a pass.

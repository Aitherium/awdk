---
name: install-skills
description: Install this skill pack into whatever agent you already use — Claude Code, OpenClaw, Hermes, Cursor, Goose, Codex, Gemini CLI, or any Agent-Skills-compatible client. Explains the two file layouts (flat slash-commands vs the SKILL.md folder standard), which agent wants which, and how to verify the agent can actually see them.
---

# install-skills — put these skills inside your agent

There are **two layouts** in the wild, and installing the wrong one is why an agent "doesn't
see" skills that are sitting right there on disk.

### Layout 1 — Agent Skills standard (`SKILL.md` in a folder)

The open standard from [agentskills.io](https://agentskills.io), originally from Anthropic,
now supported by most agents:

```
skills/
└── local-inference/
    └── SKILL.md          # frontmatter: name + description, then instructions
```

The agent reads only the `name` and `description` at startup, and loads the full body only
when a task matches. That's why the description field matters — it's the *only* thing the
agent sees when deciding whether the skill is relevant.

### Layout 2 — Claude Code slash commands (flat `.md`)

```
.claude/commands/
└── local-inference.md    # invoked as /local-inference
```

**This repo ships flat `skills/*.md`.** The installer converts to the folder layout for agents
that need it — you do not have to do that by hand.

---

## The one-command install

```bash
git clone https://github.com/Aitherium/awskills
cd awskills
bash scripts/install-awskills.sh            # macOS / Linux / WSL
pwsh -File scripts/Install-AitherSkills.ps1      # Windows PowerShell
```

It detects every agent installed on the machine, installs into each one's native layout, and
prints exactly what it wrote where. Useful flags:

```bash
bash scripts/install-awskills.sh --dry-run          # show, write nothing
bash scripts/install-awskills.sh --target openclaw  # just one agent
bash scripts/install-awskills.sh --list             # what would be detected
bash scripts/install-awskills.sh --only local-inference,ship-an-app-free
```

**Nothing is overwritten without `--force`.** An existing skill of the same name is reported
and skipped, so re-running is safe.

---

## Where each agent looks

If you'd rather do it by hand, or the installer didn't detect your agent:

| Agent | Path | Layout |
|---|---|---|
| **Claude Code** (project) | `.claude/skills/<name>/SKILL.md` | folder |
| **Claude Code** (slash command) | `.claude/commands/<name>.md` | flat |
| **Claude Code** (all projects) | `~/.claude/skills/<name>/SKILL.md` | folder |
| **OpenClaw** | `~/.openclaw/workspace/skills/<name>/SKILL.md` | folder |
| **Hermes** | `~/.hermes/skills/<name>/SKILL.md` | folder |
| **Tau** (user) | `~/.tau/skills/<name>/SKILL.md` | folder |
| **Tau** (project) | `<project>/.tau/skills/<name>/SKILL.md` | folder |
| **Any agent** (shared) | `~/.agents/skills/<name>/SKILL.md` | folder |
| **Cursor** | `.cursor/skills/<name>/SKILL.md` | folder |
| **Goose** | `~/.config/goose/skills/<name>/SKILL.md` | folder |
| **OpenCode** | `.opencode/skills/<name>/SKILL.md` | folder |
| **Gemini CLI** | `~/.gemini/skills/<name>/SKILL.md` | folder |
| **Codex** | `~/.codex/skills/<name>/SKILL.md` | folder |

> Agents move their config paths between versions. If a path above is wrong for your version,
> check that agent's own skills documentation — the *layout* is stable even when the *path*
> isn't.

**`~/.agents/skills/` is a cross-agent convention** — not owned by any one tool. Installing
there once can serve every agent that adopts it (tau already reads it). If you run several
agents on one machine, that's the install worth doing first.

**Tau enforces the folder layout and silently skips bare `.md`** — it stopped treating loose
`.md` files as skills and emits a migration hint instead. It's the clearest example of why
the layout distinction matters: the file is right there, readable, and simply never loads.

Converting one skill by hand is two commands:

```bash
mkdir -p ~/.openclaw/workspace/skills/local-inference
cp skills/local-inference.md ~/.openclaw/workspace/skills/local-inference/SKILL.md
```

That's the entire conversion. The file content is identical; only the location and filename
change.

---

## Frontmatter — what makes a skill portable

Skills in this pack that carry frontmatter work everywhere:

```markdown
---
name: local-inference
description: Run a language model on your own hardware for free — pick the right backend…
---
```

- `name` — must match the folder name. Lowercase, hyphens, no spaces.
- `description` — **write this for the agent, not for a human.** It is the entire basis on
  which the agent decides to load the skill. "Helps with models" gets ignored; "Run a language
  model on your own hardware — pick a backend, download a model that fits, serve it, prove it"
  gets loaded at the right moment.

Older skills in this pack use Claude Code's extended frontmatter (`argument-hint`,
`allowed-tools`). Other agents **ignore unknown fields** rather than erroring, so those files
are still portable — they just lose the Claude-specific behavior.

---

## Verify the agent can actually see them

Installing is not the same as loading. Restart the agent, then:

**Ask it directly:** *"List the skills you have available whose names start with 'aither'."*

It should name several. Then force one to load: *"Use the local-inference skill to tell me
which backend fits this machine."* A real load means it follows the skill's structure — the
sizing table, the checks — rather than answering from general knowledge.

**Claude Code specifically:** `/help` lists slash commands; skills in `.claude/skills/` are
loaded by description and won't appear as slash commands. Both mechanisms work; they're just
surfaced differently.

| Symptom | Cause | Fix |
|---|---|---|
| Agent names no skills | wrong layout (flat where folder was needed) | re-run installer, or convert by hand above |
| Some appear, some don't | missing/weak `description` frontmatter | add a description that says when to use it |
| Skill loads but ignores its steps | model too small to follow multi-step instructions | bigger model — see [`local-inference`](local-inference.md) |
| Installed but agent not restarted | skills are read at startup | restart the agent |

## Also wire in the live tools, not just the instructions

Skills are *instructions*. To give the agent real **tools** — GPU, local inference, files,
the platform toolset — connect an MCP server too:

- **[`openclaw`](openclaw.md)** — `aither integrate openclaw`, automated
- **[`hermes-agent`](hermes-agent.md)** — merge one config block
- **[`awnode`](aithernode.md)** — expose this machine's hardware to any MCP-capable agent
- **[`awdk`](awdk.md)** — the full toolkit

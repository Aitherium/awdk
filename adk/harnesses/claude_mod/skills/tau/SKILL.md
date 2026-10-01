---
name: tau
description: Install and run Tau, a minimalist terminal coding agent in Python — point it at your own local model via ~/.tau/catalog.toml instead of a paid API, install this skill pack into its skills directory, and use the /skill: invocation. Covers the folder-only skill layout tau enforces and the fact that tau has no MCP support, so tools come from extensions rather than an MCP server.
---

# tau — a minimalist terminal coding agent you can actually read

[Tau](https://github.com/wizzense/tau) (MIT, Python 3.12+) is a terminal coding agent: it
reads files, edits code, runs commands, and keeps session history. It's a Python port of Pi's
minimalist agent — small enough that you can read the whole thing, which makes it a good
choice when you want to *understand* your agent rather than just use it.

## Install

```bash
curl -LsSf https://twotimespi.dev/install.sh | sh     # macOS / Linux
irm https://twotimespi.dev/install.ps1 | iex          # Windows PowerShell
```

Or straight from PyPI:

```bash
uv tool install tau-ai        # preferred — tau is built around uv
pipx install tau-ai
```

Run it in any project:

```bash
cd my-project
tau                                   # interactive
tau -p "summarize the architecture"   # one-shot prompt
```

**Check:** `tau -p "reply with exactly: ok"` prints `ok`.

## Point it at your own model

Tau ships an `openai-compatible` provider kind, so the local endpoint from
[`local-inference`](local-inference.md) works directly. User providers go in
**`~/.tau/catalog.toml`**, which is overlaid on the built-in catalog:

```toml
schema_version = 1

[[providers]]
name = "local"
display_name = "Local (Ollama)"
kind = "openai-compatible"
api = "openai-completions"
base_url = "http://localhost:11434/v1"
api_key_env = "LOCAL_API_KEY"
credential_name = "local"
models = ["qwen3:8b", "llama3.2:3b"]
default_model = "qwen3:8b"
```

The fields that actually matter, and the ones that bite:

- **`kind = "openai-compatible"`** — the kind for any OpenAI-shaped endpoint. The other kinds
  (`anthropic`, `google-generative-ai`, `mistral-conversations`, `openai-codex`) speak
  different wire protocols and will not work against Ollama/vLLM/llama.cpp.
- **`api = "openai-completions"`** — chat-completions. **Not `openai-responses`**, which is
  OpenAI's newer Responses API and is *not* what local servers speak. Getting this wrong is
  the most likely reason a correct-looking local provider fails.
- **`api_key_env`** names an *environment variable*, not the key itself — never put a
  credential in this file. Local servers ignore the value, but the variable should exist:
  `export LOCAL_API_KEY=not-needed`.
- **`base_url`** — `:11434/v1` Ollama · `:8080/v1` llama.cpp · `:8000/v1` vLLM.

**Check — ask tau something, and confirm your own server served it:**

```bash
ollama ps      # the model should show as loaded while tau is answering
```

## Install this skill pack into tau

Tau reads skills from four directories, in **increasing precedence** — a project skill
overrides a user skill of the same name:

| Directory | Scope |
|---|---|
| `~/.tau/skills/` | user, tau-specific |
| `~/.agents/skills/` | user, **shared across agents** |
| `<project>/.tau/skills/` | project, tau-specific |
| `<project>/.agents/skills/` | project, shared — highest precedence |

```bash
bash scripts/install-awskills.sh --target tau             # ~/.tau/skills
bash scripts/install-awskills.sh --target agents-shared   # ~/.agents/skills
```

> ⚠️ **Tau requires the folder layout and will silently skip anything else.** A skill must be
> `<name>/SKILL.md`. A bare `<name>.md` sitting in the skills directory is **not loaded** —
> tau explicitly stopped treating bare `.md` files as skills and emits a migration hint
> pointing at `<name>/SKILL.md`. If a skill "isn't there", check the layout before anything
> else. The installer already writes the folder form.

`~/.agents/skills/` is worth knowing about beyond tau: it's a cross-agent convention, so one
install there can serve every agent that adopts it.

## Using a skill

Tau invokes skills explicitly rather than by description-matching:

```
/skill:local-inference
/skill:ship-an-app-free deploy the site in ./web
```

Anything after the skill name is passed through as additional instructions. That's a real
difference from Claude Code and OpenClaw, which decide *for themselves* when a skill is
relevant based on its `description`. In tau, **you** choose — so a skill with a mediocre
description still works here, and you can drive it deliberately.

**Check:** run `/skill:local-inference` and confirm tau follows the skill's structure (the
sizing table, the checks) rather than answering from general knowledge.

## Tools: extensions, not MCP

**Tau has no MCP support** — there is no MCP client in the codebase, so the
`aither integrate` path used by [`openclaw`](openclaw.md) does not apply. Tau's extension
point is `extensions/`, Python modules exposing a `setup` entry point, loaded from:

- `~/.tau/extensions/` — user
- `<project>/.tau/extensions/` — project, **opt-in via `--project-extensions`**

That opt-in is deliberate and worth respecting: extensions execute at session startup, so a
project extension is arbitrary code from the repo you just cloned. Tau requires you to ask
for it because it has no project trust store yet. Don't hand-wave past that flag.

Extensions can hook agent lifecycle events (`session_start`, `tool_call`, `tool_result`,
`turn_start`/`turn_end`, `compaction_start`/`end`, …), register tools, and render custom
messages — so an AitherOS bridge for tau is written as an extension, not an MCP server.

## Project instructions

Tau reads `AGENTS.md` from the project root, plus `.tau/SYSTEM.md` for system-prompt content.
Keep them short — they cost context on **every** turn, unlike skills, which load only when
invoked. Anything procedural belongs in a skill.

Sessions are JSONL under `~/.tau/sessions/`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Skill not found by `/skill:name` | bare `.md` instead of `<name>/SKILL.md` | use the folder layout |
| Local provider errors on every call | `api = "openai-responses"` | use `openai-completions` |
| Provider ignored entirely | wrong `kind` for the endpoint | `openai-compatible` for local servers |
| Wrong skill loads | a higher-precedence project skill shadows it | check all four dirs; project wins |
| Project extension never runs | not opted in | pass `--project-extensions` |
| Looking for MCP config | tau has none | write an extension instead |

## Next

- **[`local-inference`](local-inference.md)** — the endpoint tau points at
- **[`install-skills`](install-skills.md)** — layouts and paths for every other agent
- **[`ods`](ods.md)** — a whole local AI stack, if you'd rather not wire services by hand
- **[`aither-start`](aither-start.md)** — the guided path this plugs into

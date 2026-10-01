---
name: aither-start
description: Zero-to-working-agent on your own machine, for someone who has never written code. Detects the hardware, installs the agent toolkit, downloads a model that actually fits, wires it into whatever agent you already use, and proves it works with a real round-trip. The front door to every other skill in this pack.
---

# aither-start — you have a computer and an AI agent; let's make it yours

This is the **front door**. You do not need to know how to code. You need a machine you can
install things on, and an agent (Claude Code, OpenClaw, Hermes, Cursor, Goose…) that can run
shell commands for you.

Read this skill top-to-bottom and do what it says, in order. Every step ends in a check that
either passes or fails out loud. **If a check fails, stop and fix it before moving on** —
each step assumes the one before it actually worked.

## What you'll have at the end

| | |
|---|---|
| An agent toolkit on your machine | `adk` — your agent, your loop, your data |
| A language model running locally | free, offline, no API bill |
| Your existing agent wired to it | Claude Code / OpenClaw / Hermes / Cursor / Goose |
| The rest of this pack installed | ~30 skills your agent can now use |

Total cost: **$0**. Nothing here requires a paid API key.

---

## Step 0 — what machine is this?

Ask the agent to run this. It's read-only; it changes nothing.

```bash
# OS + CPU + memory
uname -a 2>/dev/null || echo "Windows"
python3 -c "import os; print('cores:', os.cpu_count())" 2>/dev/null || python -c "import os; print('cores:', os.cpu_count())"

# GPU, if any (silence is fine — CPU-only works)
nvidia-smi --query-gpu=name,memory.total --format=csv 2>/dev/null || echo "no NVIDIA GPU"
```

**Write down the memory number.** Everything below branches on it:

| What you have | What you can run | Go to |
|---|---|---|
| Any laptop, no GPU, ≥8 GB RAM | a small model, slowly but usably | `local-inference` → Ollama |
| ≥16 GB RAM, no GPU | a good 7–14B model at reading speed | `local-inference` → Ollama |
| NVIDIA GPU ≥8 GB VRAM | a fast 7–14B model | `local-inference` → Ollama or vLLM |
| NVIDIA GPU ≥24 GB VRAM | 27–32B models, or serve to others | `local-inference` → vLLM |
| Nothing that fits | someone else's spare GPU on the mesh | `omninode-node` |

> **You are not stuck if your machine is weak.** A 4 GB model on a 5-year-old laptop still
> answers questions, writes code, and drives every skill in this pack. Start there.

## Step 1 — install the toolkit

```bash
pip install awdk
adk onboard --quick
```

`--quick` detects your hardware, stands up inference, installs an agent pack, and enrolls the
machine. It asks before anything irreversible.

**Check — this must print a version, not an error:**

```bash
adk --version
```

*No `pip`?* Install Python 3.11+ first: [python.org/downloads](https://www.python.org/downloads/)
(tick **"Add Python to PATH"** on Windows). Then re-run.

*`pip` works but `adk` isn't found?* Your shell can't see Python's script directory. Use
`python -m adk` everywhere below, or ask your agent to fix PATH.

## Step 2 — get a model running locally

Full detail lives in the **[`local-inference`](local-inference.md)** skill — read it now, do
what it says, come back here. The 30-second version, which works on almost anything:

```bash
curl -fsSL https://ollama.com/install.sh | sh    # macOS/Linux; Windows: ollama.com/download
ollama pull qwen3:8b                             # ~5 GB — the safe default
ollama run qwen3:8b "reply with exactly: ok"
```

**Check — that last command must print `ok`.** If it printed nothing, hung, or died, you
picked a model too big for your memory. Drop to `qwen3:4b`, or `gemma3:1b` on a very small box.

## Step 3 — install this skill pack into your agent

One command, and your agent gains every skill in this repo:

```bash
git clone https://github.com/Aitherium/awskills
cd awskills
bash scripts/install-awskills.sh          # Windows: pwsh -File scripts/Install-AitherSkills.ps1
```

It detects which agents you have and installs in each one's native format. See
**[`install-skills`](install-skills.md)** for per-agent paths, doing it by hand, and what to do
when your agent isn't auto-detected.

**Check — restart your agent, then ask it:** *"list the aither skills you can see"*. It should
name several. If it names none, the install went somewhere your agent doesn't read — the
`install-skills` skill has the per-agent table.

## Step 4 — point your agent at your own model

Now your agent uses **your** local model instead of a paid API. Pick your agent:

| Agent | Do this |
|---|---|
| **OpenClaw** | `aither integrate openclaw` — automated. See [`openclaw`](openclaw.md) |
| **Hermes** | `hermes model` → custom endpoint. See [`hermes-agent`](hermes-agent.md) |
| **Claude Code** | Keep Claude for reasoning; the local model serves the skills. Nothing to change |
| **Cursor / Goose / other** | Point its OpenAI-compatible base URL at `http://localhost:11434/v1` |

**Check — ask your agent a question and watch `ollama ps` in another terminal.** If the model
shows as loaded while it answers, it's really running on your machine.

## Step 5 — build and ship something

You now have an agent that can run code. Use it:

- **[`ship-an-app-free`](ship-an-app-free.md)** — idea → working app → public URL, on free
  tiers only. No credit card, no server to rent.
- **[`graph-rag-agent`](graph-rag-agent.md)** — point it at your own documents and ask questions
  that get answered from *your* material.
- **[`awdk`](awdk.md)** — the full toolkit: agent packs, memory, tools.

## Step 6 — optional: let others use your spare compute

If your machine has a GPU that idles, it can serve inference to the mesh and earn settlement
for it: **[`omninode-node`](omninode-node.md)** and **[`awnode`](aithernode.md)**. Entirely
optional and off by default — nothing joins a network unless you run the command.

---

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Model load kills the machine / swaps forever | model bigger than free RAM | smaller model or heavier quantization — [`local-inference`](local-inference.md) |
| `adk: command not found` after a clean install | Python scripts dir not on PATH | use `python -m adk`, or fix PATH |
| Agent doesn't see the skills | installed in the wrong layout for that agent | [`install-skills`](install-skills.md) — the format differs per agent |
| Local model answers but ignores tools | model has no tool-calling, or the parser flag is missing | [`local-inference`](local-inference.md) § tool calling |
| Everything is slow but works | CPU inference, normal | fine for skills; add a GPU later |

**Read the check at each step, and believe it over how it feels.** "It printed something" is not
"it worked" — the whole point of the checks is that they can fail.

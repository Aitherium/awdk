---
name: adk-unsloth
description: >-
  Local training and serving, wired to your agent. [Unsloth](https://github.com/unslothai/unsloth) trains LoRA/QLoRA 2x faster with ~70% less VRAM and serves GGUF locally. Pairing it with the ADK gives you a loop that runs entirely on your own hardware: your agent serves from a local model, you fine-tune on what it produced, you swap the adapter, you measure again.
---

# adk-unsloth — local training and serving, wired to your agent

[Unsloth](https://github.com/unslothai/unsloth) trains LoRA/QLoRA 2x faster with ~70%
less VRAM and serves GGUF locally. Pairing it with the ADK gives you a loop that runs
entirely on your own hardware: your agent serves from a local model, you fine-tune on
what it produced, you swap the adapter, you measure again.

This skill covers how to wire the two together **and where the licence line is** —
which is the part that decides your architecture, so read that section before you
design anything around it.

## The licence line (read this first)

Unsloth is dual-licensed and the split is finer than the README suggests:

| tree | licence | what it means for you |
|---|---|---|
| the `unsloth` training library | **Apache-2.0** | ordinary dependency — `pip install unsloth`, import it, ship your product |
| Unsloth Studio (backend + desktop UI) | **AGPL-3.0** | do **not** vendor, copy or link it into a proprietary product |

Measured per-file, the AGPL half is nearly everything outside the training library —
the Studio backend, the desktop app, and most of the CLI. If you are building a
commercial product, the only safe shape is:

> **Run Studio as its own unmodified process and talk to it over its
> OpenAI-compatible HTTP API.**

You are then a *user* of the program, not a distributor of a derivative of it. Do not
copy its source into your codebase, and do not import its Studio modules. Reimplement
an idea if you want it in your own code; that is always allowed, and expression is
what copyright covers.

## Serve a local model to your agent

Start Unsloth and load a model, then point the ADK at it as an OpenAI-compatible
backend:

```bash
unsloth studio -p 8888
```

```bash
adk backend list                  # what the ADK already detects
adk backend guide                 # step-by-step for a specific backend
adk backend switch                # change the live backend, no restart
adk backend test                  # prove it actually answers
adk backend status                # current config + connectivity
```

**Run `adk backend test` before believing any of it.** A configured endpoint that
never answers looks identical to a working one until the first real turn — a model
list is a menu, not a heartbeat.

## Let a coding agent use the local model

Unsloth ships its own bridge that points Claude Code, Codex, OpenCode and others at
the local server:

```bash
unsloth start claude
unsloth start codex
unsloth start claude --as-subagent --model unsloth/model-GGUF:quant
```

`--as-subagent` is the interesting one: the parent agent keeps its frontier model and
delegates only sub-tasks to the local one. That is the same delegation shape the ADK's
own harness plane uses (see `adk-harnesses`), so pick one and be deliberate about
which — running both means two independent paths to inference, with two sets of
budget and routing behaviour, and a surprise when they disagree.

## Train on what your agent produced

The loop worth building:

1. **Harvest** — your agent's accepted turns, tool calls and diffs become a dataset.
2. **Fine-tune** — Unsloth trains a LoRA on it.
3. **Serve** — load the adapter, point the ADK at it.
4. **Measure** — and this is the step everyone skips.

```bash
unsloth studio -p 8888            # Data Recipes: PDF/CSV/JSON -> dataset, then train
```

**Measure with something that can fail.** A fine-tune always produces a model, and a
loss curve always goes down; neither tells you the model got better at your job. Use
a held-out set the model has never seen, and prefer a metric with an execution behind
it (does the generated code *run*?) over one scored by string match. A training loop
whose reward is a regex will faithfully optimise the regex.

## Remote access, and the footgun in it

```bash
unsloth studio --secure                       # HTTPS via a Cloudflare tunnel; port stays local
unsloth studio -H 0.0.0.0 -p 8888             # raw port on all interfaces — trusted networks only
```

Server-side tools (web search, Python and terminal execution) run **as your user** and
are on by default. Anyone who reaches the server with the API key can run code on that
machine. Pass `--disable-tools` when exposing it, keep the key private, and set a real
admin password before the first public launch rather than after.

## Traps

- **A model list is not liveness.** `/v1/models` answers from a config file. It will
  happily list a model the server cannot load, and your agent then hangs on the first
  real request. Always `adk backend test`.
- **VRAM is not additive with your agent.** If the ADK is serving from the same GPU
  you are training on, the trainer will take what it needs and the serving model gets
  evicted mid-conversation. Quiesce inference before a training run, or use different
  devices.
- **GGUF quant choice changes behaviour, not just size.** A model that tool-calls
  correctly at 8-bit can start emitting malformed calls at 4-bit. If tool calls
  suddenly stop working after a re-quant, suspect the quant before the prompt.

## See also

- `adk-harnesses` — driving Claude Code / Codex / others from your agent
- `llm-serving` — serving models generally
- `awdk` — the SDK

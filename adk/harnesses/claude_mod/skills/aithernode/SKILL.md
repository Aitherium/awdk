---
name: aithernode
description: >-
  Awnode — turn a machine's hardware into something your agents can use. awnode is the body of AitherOS. It's a local [MCP](https://modelcontextprotocol.io) server that exposes a machine's real capabilities — GPU/CPU stats, local LLM inference (Ollama / LM Studio), image generation (ComfyUI: Flux, SDXL, Pony), and safe filesystem access — so Aither agents can *act* on that hardware instead of just talking about it. If awdk is the mind, awnode is the hands.
---

# awnode — turn a machine's hardware into something your agents can use

**awnode is the body of AitherOS.** It's a local [MCP](https://modelcontextprotocol.io) server
that exposes a machine's real capabilities — GPU/CPU stats, local LLM inference (Ollama / LM Studio),
image generation (ComfyUI: Flux, SDXL, Pony), and safe filesystem access — so Aither agents can
*act* on that hardware instead of just talking about it. If awdk is the mind, awnode is the
hands.

## Set it up

With [awdk](awdk.md) installed (`pip install awdk`):

```bash
adk mcp node          # start the lightweight local MCP server exposing this box's hardware
```

That's the whole first mile: the node advertises what this machine can do (GPU, inference backends,
ComfyUI if present) over MCP, and any agent you point at it can call those capabilities.

## Stand it up as an inference node (GPU or CPU)

To make the box *serve a model* to the fleet — not just expose stats — use the hardware-aware
bootstrap. It detects the hardware, matches a deployment recipe (vLLM, Ollama, llama.cpp, or a cloud
API), applies it, and registers the backend so routers can find it:

```bash
adk onboard --quick                       # detect hardware → pick backend → serve → verify
# or, driven from AitherZero as an idempotent, remote-or-local playbook:
Invoke-AitherPlaybook bootstrap-inference-node
Invoke-AitherPlaybook bootstrap-inference-node -Variables @{ NodeIp='192.168.1.121'; RecipeId='cpu-1bit-llamacpp' }
```

Recipes match the hardware automatically: `cuda-dual-stack-32gb` (RTX 5090, co-resident models),
`cuda-vllm-24gb` / `cuda-vllm-8gb`, `cpu-1bit-llamacpp` (tiny boxes via llama.cpp), `cpu-ollama`.
Every step is idempotent — safe to re-run.

## Use it

Once running, the node shows up wherever your agents look:

```bash
adk doctor            # confirms the node's inference + capabilities are healthy
adk chat              # your agent can now generate images / run local inference on this box
```

Point a hosted agent at the node's MCP endpoint and it gains local image-gen and inference as tools.

## Part of one substrate

An awnode is a machine made useful; [awconnect](aitherconnect.md) wires it to the control
plane and mesh so agents elsewhere can reach it, [awdk](awdk.md) is the runtime that
An AitherNode is a machine made useful; [Awconnect](awconnect.md) wires it to the control
plane and mesh so agents elsewhere can reach it, [awdk](awdk.md) is the runtime that
drives it, [AitherZero](aitherzero.md) provisions the box underneath it, and
[OmniNode](omninode-node.md) pools several nodes into one compute fabric. One motion, not five.

MIT-licensed, like everything in `awskills`.

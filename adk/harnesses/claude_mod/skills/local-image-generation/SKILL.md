---
name: local-image-generation
description: Give an agent the ability to draw — generating images on the machine it is already running on, via ComfyUI, Sana, SD.Next/A1111 or an in-browser model. No hosted API, no key, no prompt leaving the box. Use when an agent needs a picture, when "add image generation" comes up, or when a local image backend is running and nothing is using it.
---

# local-image-generation — the agent draws on your own hardware

An agent that can write code, run it, and read the result still cannot show you
anything. The reflex is to reach for a hosted image API: it costs money per
picture, it needs a key, and it ships every prompt off the machine.

If you have a GPU you very likely already have an image server on it. This skill
wires an agent to **that**, over loopback, so the capability costs nothing and
nothing leaves the box.

**No GPU is not a blocker.** Every backend here runs on CPU too — slower, and
completely usable. See *No GPU? Run it on CPU* below for the real timings.

## What it talks to

Discovery is automatic and in this order. All loopback, all optional.

| lane | default port | notes |
|---|---|---|
| ComfyUI | 8188 | the one most people already have; best quality |
| Sana | 8202 | small and fast, if you serve it |
| SD.Next / A1111 | 7860 | the other common local server |

**Nothing is installed or started for you.** This is detection only — an agent
that silently launches a multi-gigabyte model on your GPU because it wanted a
picture is a worse agent.

Running one on a different port? Say so, or it will be reported as *not
running* by a probe that never looked there:

```bash
export ADK_COMFYUI_PORT=8189
export ADK_SANA_PORT=8203
export ADK_SDNEXT_PORT=7861
```

A value that is not a port number is ignored and the default is used, so a typo
degrades to the normal behaviour rather than probing port 0 and reporting
everything down.

## Find out what you already have

```bash
adk image --backends
```

```
  [READY] comfyui  127.0.0.1:8188  ready (HTTP 200)
  [  -  ] sana     127.0.0.1:8202  not running
  [  -  ] sdnext   127.0.0.1:7860  not running
```

Read the third state carefully, because it is the one that saves you an hour:

```
  [  -  ] sana     127.0.0.1:8202  running, but no image route (HTTP 404)
```

That means a server **is** answering on that port and cannot generate. It is not
"not running", and fixing it is a different job. See *Why the probe does not ask
`/health`* below — this distinction was earned the hard way.

## Draw something

```bash
adk image "a brass astrolabe on a dark wooden desk, candlelight"
```

Writes `adk-image.png` next to you. Useful flags:

```bash
adk image "a goblin at a green CRT" \
  --width 768 --height 768 \
  --steps 6 --cfg 2 \
  --model sdxl_lightning_4step.safetensors \
  --negative "blurry, text" \
  --seed 12345 \
  --out ./goblin.png
```

`--model` picks a checkpoint when the backend has several; omit it and the first
one is used. `--backend <id>` forces a lane instead of taking the first ready one.

## No GPU? Run it on CPU

ComfyUI takes `--cpu` and needs nothing else — no CUDA, no drivers, no special
build. Everything above works unchanged; only the wait differs.

```bash
python main.py --cpu --listen 127.0.0.1 --port 8188
```

`adk image --backends` tells you which one you are on, so a slow generation is
never a mystery:

```
  [READY] comfyui  127.0.0.1:8188  ready (HTTP 200)  [cuda:0 NVIDIA GeForce RTX 5090]
  [READY] comfyui  127.0.0.1:8189  ready (HTTP 200) -- on CPU, expect ~10x slower  [cpu]
```

### What it actually costs

Measured on one machine (32 cores + an RTX 5090), same command, only the port
differing — so the two columns are the same work on the same box:

| model | size | steps | GPU | CPU |
|---|---|---|---|---|
| SDXL-lightning | 768² | 4–6 | **9 s** | **107 s** |
| SD 1.5 | 512² | 12 | — | **85 s** |

About an order of magnitude. That is a coffee, not a blocker — and it is one
image, not a batch.

### Making CPU pleasant

- **Prefer an SD 1.5-class model over SDXL.** Roughly half the work per step
  at 512², and on CPU that is the difference between waiting and giving up.
- **Use a few-step model.** Lightning/Turbo checkpoints do 4–6 steps instead of
  20–30; the step count is what you are paying for.
- **Generate at 512², upscale after.** Cost scales with pixels.
- **Set the thread count if the box is shared.** ComfyUI inherits torch's
  default, which will happily take every core:
  ```bash
  OMP_NUM_THREADS=8 python main.py --cpu
  ```

### If you have an Apple Silicon Mac

Do not use `--cpu` — it will work and be needlessly slow. ComfyUI uses the
`mps` backend automatically on Apple Silicon, which is much faster than the CPU
path. `adk image --backends` reports `[mps]` when that is what you are on.

## Give it to an agent over HTTP

The harness daemon exposes the same thing on the OpenAI shape, so any client
that already speaks that protocol works unchanged:

```
GET  /v1/images/backends      which lanes can actually generate
POST /v1/images/generations   {"prompt": "...", "size": "768x768"}
```

```bash
curl -s -X POST http://127.0.0.1:9001/v1/images/generations \
  -H "Authorization: Bearer $ADK_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a small brass robot holding a wrench","size":"512x512"}' \
  | python -c "import sys,json,base64;open('robot.png','wb').write(base64.b64decode(json.load(sys.stdin)['data'][0]['b64_json']))"
```

A failure here is **503, not 500** — every one of them means "no local backend
can do this right now", which is an availability answer, and its message names
the lane and the port. A 500 would read as a daemon bug and send you to the
wrong logs.

## In Python

```python
from adk.images import ImageRequest, generate, discover, ImageError

lanes = await discover()                       # never raises
if not any(ln.up for ln in lanes):
    ...                                        # nothing can generate

try:
    out = await generate(ImageRequest(prompt="a lighthouse in fog", steps=20))
except ImageError as e:
    print(e)                                   # written to be shown to a person
else:
    png = base64.b64decode(out["images_b64"][0])
```

## Three things that will otherwise cost you an afternoon

### Why the probe does not ask `/health`

A liveness probe is the obvious design and it is wrong. Measured on a real
daemon: it answered `/health` with **200** and `/v1/images/generations` with
**404**. A `/health` probe therefore reports the backend UP, routing lands on
it, and the caller gets a 404 instead of a picture.

`/health` is a **menu**. It tells you a process is alive, never that it can do
the one thing you are about to ask for. So each candidate is probed on the route
generation actually uses, and a 404 there means *not capable* even though the
server is plainly running.

### Never forward `Origin` to a local image server

If you put a reverse proxy in front of ComfyUI — serving a UI on another port,
say — do not pass the browser's `Origin` header upstream. Measured against
ComfyUI 0.3.71:

| request | result |
|---|---|
| `POST /prompt`, no Origin | **400** — a graph error; it read the body |
| same POST, foreign Origin | **403** — refused before reading the body |

Nothing in that failure names a header, so it reads as "the backend rejected my
job" and you go looking at your JSON. A loopback reverse proxy is not a
cross-origin caller; strip `Origin` and `Referer`.

### An image server keeps its checkpoints resident

After a job, ComfyUI holds the model in VRAM. On a single-GPU box that is
memory your LLM cannot have — and an LLM server typically allocates its KV cache
**once at startup and cannot grow later**, so the failure is not "slow", it is
the LLM refusing to start at all with something like *no available memory for
the cache blocks*, restart-looping while an image generator nobody is using sits
on the VRAM.

Two ways out, and pick deliberately:

```bash
# give the image server a ceiling, leaving headroom for everything else
python main.py --reserve-vram 10

# or free it on demand, between jobs
curl -X POST http://127.0.0.1:8188/free \
  -H 'Content-Type: application/json' \
  -d '{"unload_models":true,"free_memory":true}'
```

If you lower a reservation, **restart the LLM first and confirm it came up** —
otherwise you trade a slightly faster image for a dead model server, and you
will not find out until the next request.

## When nothing is available

The error names every port it tried and what to start. That is deliberate: "no
image backend" with no ports in it is a dead end for whoever reads it.

```
No local image backend is able to generate. Tried: ComfyUI (127.0.0.1:8188) --
not running, Sana (127.0.0.1:8202) -- not running. Start ComfyUI (default port
8188) and try again. Nothing is downloaded or installed for you.
```

An empty result is never rendered as a successful empty result. A generate call
that returns a 200 with no image raises instead, because a picture that silently
did not arrive looks exactly like a model with no ideas.

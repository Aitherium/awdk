---
name: local-inference
description: Run a language model on your own hardware for free — pick the right backend (Ollama, llama.cpp, or vLLM) for the machine you actually have, download and quantize a model that fits in memory, serve it on an OpenAI-compatible endpoint, and prove it works with a real round-trip. Covers tool-calling/function-calling setup and the failure modes that look like success.
---

# local-inference — a model on your box, for $0

Three backends. **Pick one.** They are not competitors; they are different points on the
"easy ↔ fast" line, and picking wrong is the single most common way this goes badly.

| Backend | Use it when | Skip it when |
|---|---|---|
| **Ollama** | You want it working in 5 minutes. Any OS, GPU optional. | You need max throughput or many concurrent users |
| **llama.cpp** | CPU-only, tiny RAM, exotic hardware, or you need a specific GGUF quant | A simpler tool would do |
| **vLLM** | NVIDIA GPU, serving others, high throughput | No GPU — vLLM on CPU is not the point of vLLM |

> **The rule that saves you a wasted afternoon:** the model must fit in memory *with room to
> spare for its context window*. A "fits exactly" model does not fit. Leave ~20% headroom.

---

## Sizing — do this before downloading anything

```bash
# free RAM (GB)
free -g 2>/dev/null | awk '/Mem:/{print "RAM free:", $7}' || vm_stat 2>/dev/null | head -2
# VRAM (GB), if you have an NVIDIA GPU
nvidia-smi --query-gpu=name,memory.free --format=csv 2>/dev/null || echo "no NVIDIA GPU"
```

Rough download size of a 4-bit quantized model — **the number that must fit**:

| Parameters | 4-bit size | Runs on |
|---|---|---|
| 1–3 B | 1–2 GB | anything, including a phone |
| 7–8 B | 4–5 GB | 8 GB RAM laptop, or 8 GB VRAM |
| 12–14 B | 7–9 GB | 16 GB RAM, or 12 GB VRAM |
| 27–32 B | 16–20 GB | 32 GB RAM, or 24 GB VRAM |
| 70 B+ | 40 GB+ | 64 GB RAM, or 2× 24 GB VRAM |

Add **1–4 GB for the KV cache** depending on how long your conversations get. That is the
part everyone forgets, and it is why a model that loaded fine dies twenty messages in.

---

## Path A — Ollama (start here)

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh
# Windows: download the installer from https://ollama.com/download
```

Pull a model sized from the table above:

```bash
ollama pull qwen3:8b            # ~5 GB — good default, tool-calling capable
ollama pull qwen3:4b            # ~2.5 GB — small machines
ollama pull gemma3:1b           # ~800 MB — very small machines
ollama pull llama3.1:8b         # ~5 GB — alternative
```

**Prove it — this must print `ok` and nothing else:**

```bash
ollama run qwen3:8b "reply with exactly: ok"
```

Ollama serves an **OpenAI-compatible API** at `http://localhost:11434/v1` with no extra work.
That URL is what every agent in this pack wants:

```bash
curl -s http://localhost:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"say ok"}]}' \
  | head -c 400
```

**A real round-trip means JSON containing a `choices[0].message.content`.** A `200` with an
error body inside is not success — read the body, not the status code.

## Path B — llama.cpp (CPU-only, or you need a specific quant)

```bash
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build && cmake --build build --config Release -j
```

Get a GGUF file from Hugging Face (any repo whose name ends in `-GGUF`):

```bash
pip install -U "huggingface_hub[cli]"
hf download bartowski/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf --local-dir ./models
```

Which quant to take, in one line each:

| Quant | Meaning |
|---|---|
| `Q8_0` | near-lossless, 2× the size of Q4 — use if it fits |
| `Q5_K_M` | very good quality, moderate size |
| **`Q4_K_M`** | **the default everyone should start with** — best quality/size trade |
| `Q3_K_M` | noticeably degraded; use only if Q4 won't fit |
| `Q2_K` / `IQ1` | last resort; expect real quality loss |

Serve it OpenAI-compatible on port 8080:

```bash
./build/bin/llama-server -m ./models/Qwen3-8B-Q4_K_M.gguf \
  -c 8192 --host 0.0.0.0 --port 8080
```

`-c` is the context window. **Raising it costs RAM** — it is the most common cause of an
out-of-memory kill that "worked yesterday".

**Prove it:**

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"say ok"}]}' | head -c 400
```

## Path C — vLLM (NVIDIA GPU, serving throughput)

```bash
pip install vllm
```

Serve, sized to your GPU:

```bash
vllm serve Qwen/Qwen3-8B \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90
```

Flags that matter, and what they actually do:

- `--gpu-memory-utilization` — fraction of VRAM vLLM may claim. **Lower it to `0.80` if
  anything else uses the GPU** (a desktop session, a browser). At `0.95` on a GPU driving a
  display you will OOM on the first long prompt.
- `--max-model-len` — context window. Same warning as llama.cpp: it is a VRAM multiplier.
- `--quantization awq` (or `gptq`) — for a pre-quantized repo. **Don't pass it for an
  unquantized repo** — vLLM will fail rather than quantize on the fly.
- `--dtype bfloat16` — the default on modern GPUs; drop to `float16` on pre-Ampere cards.
- `--enforce-eager` — disables CUDA graphs. Slower, but the first thing to try when a model
  loads and then produces garbage or crashes on generation.

**Prove it** (vLLM serves on `:8000` by default):

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"say ok"}]}' | head -c 400
```

---

## Tool calling (needed for agents to actually *do* things)

An agent that can chat but can't call tools is a chatbot. Tool calling needs **both** a model
trained for it **and** the right server-side parser.

**Ollama** — built in for tool-capable models (`qwen3`, `llama3.1`, `mistral-nemo`). Nothing
to configure. If tool calls come back as literal text in the reply, the model isn't
tool-trained — switch models rather than fighting the parser.

**vLLM** — you must name the parser that matches the model's template:

```bash
vllm serve <model> --enable-auto-tool-choice --tool-call-parser hermes
```

| Parser | Model families |
|---|---|
| `hermes` | Nous Hermes, Qwen, and many fine-tunes that adopted the Hermes template |
| `llama3_json` | Llama 3.x instruct |
| `mistral` | Mistral / Mixtral instruct |

**The wrong parser fails silently in the worst way:** the model emits a perfectly good tool
call, the server fails to parse it, and it reaches your agent as chat text. Symptom — the
agent "talks about" calling a tool but never calls one. Fix the parser, not the prompt.

**llama.cpp** — pass `--jinja` so `llama-server` uses the model's own chat template; without
it, tool-call formatting is wrong for most models.

---

## Making it smaller — quantize it yourself

If the model you want is 2 GB too big, quantize instead of giving up. `tools/quantize_model.py`
in this repo does 4-bit AutoRound **free on local CPU+GPU** (`--iters 0` = RTN, the default),
keeps `lm_head` and multimodal projectors in bf16, and exports `compressed-tensors`:

```bash
python tools/quantize_model.py <hf-model-id> --out ./quantized --dry-run   # preview
python tools/quantize_model.py <hf-model-id> --out ./quantized
```

Deeper treatment, including when calibrated mode is worth it and which architectures it
refuses: the [`model-quantization`](model-quantization.md) skill.

For GGUF specifically, `llama.cpp` converts and quantizes directly:

```bash
python llama.cpp/convert_hf_to_gguf.py <local-model-dir> --outfile model-f16.gguf
./llama.cpp/build/bin/llama-quantize model-f16.gguf model-Q4_K_M.gguf Q4_K_M
```

---

## Failure modes that look like success

| Symptom | What's actually wrong | Fix |
|---|---|---|
| Machine freezes / OOM-killed on load | model + KV cache > free memory | smaller model, lower `-c`/`--max-model-len`, heavier quant |
| Loads, then emits repeated or garbled tokens | wrong quant for the architecture, or a bad CUDA-graph path | try `Q4_K_M`; on vLLM add `--enforce-eager` |
| Agent describes calling a tool but never does | tool-call parser mismatch | set the right `--tool-call-parser`; `--jinja` on llama.cpp |
| `200 OK` but empty/error content | error body behind a success status | read the body — never trust the status alone |
| Fast on short prompts, dies on long ones | KV cache growth | raise headroom or lower context |
| GPU shows free VRAM but vLLM OOMs | another process (display) holds VRAM | lower `--gpu-memory-utilization` to `0.80` |
| First token takes 30+ s every time | model reloaded per request | keep the server resident; don't spawn per call |

## Where to go next

- **[`aither-start`](aither-start.md)** — the guided path this skill plugs into
- **[`install-skills`](install-skills.md)** — point your agent at the endpoint you just proved
- **[`bonsai-27b`](bonsai-27b.md)** — a 27B model on a plain CPU box via 1-bit quantization
- **[`split-inference`](split-inference.md)** — one model split across several machines
- **[`omninode-node`](omninode-node.md)** — no hardware? use the mesh

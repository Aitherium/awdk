---
name: bonsai-27b
description: >-
  Run a 27B model in ~3.8 GB on a plain CPU box. [Bonsai-27B](https://huggingface.co/prism-ml/Bonsai-27B-gguf) (by PrismML) is a 27-billion- parameter model quantized to 1 bit per weight (Q1_0, ~1.1 bit/weight) — the whole thing is a 3.8 GB GGUF that runs on an ordinary CPU with 8 GB of RAM. No GPU, no cloud. This skill takes a spare machine from nothing installed to a live OpenAI-compatible endpoint serving Bonsai.
---
# bonsai-27b — run a 27B model in ~3.8 GB on a plain CPU box

[Bonsai-27B](https://huggingface.co/prism-ml/Bonsai-27B-gguf) (by **PrismML**) is a 27-billion-
parameter model quantized to **1 bit per weight** (`Q1_0`, ~1.1 bit/weight) — the whole thing is a
**3.8 GB GGUF** that runs on an ordinary CPU with 8 GB of RAM. No GPU, no cloud. This skill takes a
spare machine from nothing installed to a live OpenAI-compatible endpoint serving Bonsai.

> **Successor:** [bonsai2-27b](bonsai2-27b.md) — Bonsai 2 27B (PTQ1_0 5.9 GB / PQ2_0 7.2 GB,
> 12 GB+ RAM) is the better model and the one the platform's installers now pick for the 27B
> tier. This page stays for the 8 GB-class box and for existing Q1_0 deployments.

**One catch:** mainline llama.cpp and Ollama **cannot** load `Q1_0` — the 1-bit format needs
PrismML's own [llama.cpp fork](https://github.com/PrismML-Eng/llama.cpp) (the `prism` branch). This
skill builds that fork; everything else is standard llama.cpp.

## Set it up

On a Linux box (or Windows WSL2 Ubuntu) with 12+ cores, ~8 GB free RAM, ~10 GB free disk:

```bash
# 1. build PrismML's llama.cpp fork (mainline can't load Q1_0)
sudo apt-get update && sudo apt-get install -y build-essential cmake libcurl4-openssl-dev git wget
git clone https://github.com/PrismML-Eng/llama.cpp ~/llama.cpp
cd ~/llama.cpp && git checkout prism
cmake -B build -DGGML_CUDA=OFF -DGGML_NATIVE=ON -DLLAMA_CURL=ON
cmake --build build -j"$(nproc)" --target llama-server llama-cli

# 2. fetch the 1-bit weights (3.8 GB) + the multimodal projector
mkdir -p ~/models/bonsai && cd ~/models/bonsai
wget https://huggingface.co/prism-ml/Bonsai-27B-gguf/resolve/main/Bonsai-27B-Q1_0.gguf
wget https://huggingface.co/prism-ml/Bonsai-27B-gguf/resolve/main/Bonsai-27B-mmproj-Q8_0.gguf

# 3. serve an OpenAI-compatible endpoint on :8090
~/llama.cpp/build/bin/llama-server \
  -m ~/models/bonsai/Bonsai-27B-Q1_0.gguf \
  --mmproj ~/models/bonsai/Bonsai-27B-mmproj-Q8_0.gguf \
  --host 0.0.0.0 --port 8090 -t 12 -c 4096 --no-warmup
```

## Use it

```bash
# health
curl http://localhost:8090/health

# a completion (OpenAI-compatible /v1/chat/completions)
curl -s http://localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "messages": [{"role":"user","content":"Explain 1-bit quantization in two sentences. /no_think"}],
  "max_tokens": 128
}'
```

Any OpenAI-compatible client works — point its base URL at `http://<host>:8090/v1`.

## Keep it running (durable service)

To survive reboots, wrap `llama-server` in a systemd unit (`Restart=always`):

```ini
# ~/.config/systemd/user/bonsai-llama.service
[Unit]
Description=Bonsai 27B Q1_0 (1-bit) llama-server
[Service]
ExecStart=%h/llama.cpp/build/bin/llama-server -m %h/models/bonsai/Bonsai-27B-Q1_0.gguf --mmproj %h/models/bonsai/Bonsai-27B-mmproj-Q8_0.gguf --host 0.0.0.0 --port 8090 -t 12 -c 4096 --no-warmup
Restart=always
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now bonsai-llama
```

## What to expect

- **Speed**: CPU inference is ~4 tokens/sec — great for background/reasoning tasks, not chat you
  watch type. It's a *reasoning* model and emits think tokens; add `/no_think` to a prompt to skip.
- **Footprint**: 3.8 GB on disk, ~6–8 GB resident. That's a 27B model on a laptop.
- **The trick**: 1-bit `Q1_0` is why it fits — and why it needs the PrismML fork, not mainline.
- **GPU?** A conventional 4-bit AWQ build of Bonsai exists too (~17.5 GB) if you have the VRAM; the
  1-bit CPU path here is the one that runs anywhere.

## Part of one substrate

A Bonsai box is a cheap always-on reasoning [awnode](aithernode.md); join it to a fleet with
[AitherMesh](aithermesh.md), point [awdk](awdk.md) agents at its `:8090/v1` endpoint, and
provision the host with [AitherZero](aitherzero.md).

Model + fork © **PrismML** — <https://huggingface.co/prism-ml/Bonsai-27B-gguf>. This skill is
MIT-licensed, like everything in `awskills`.

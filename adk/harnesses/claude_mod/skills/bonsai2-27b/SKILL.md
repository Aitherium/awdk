---
name: bonsai2-27b
description: "Serve PrismML's Bonsai 2 27B (Ternary-Bonsai-2-27B, PTQ1_0 5.9 GB or PQ2_0 7.2 GB) as an OpenAI-compatible endpoint with the PrismML llama.cpp fork. Use on 'run Bonsai 2 locally', 'self-host a 27B on a GPU or CPU box', 'why does Bonsai 2 output gibberish'. Stock llama.cpp cannot serve it."
---

# bonsai2-27b — a 27B reasoning model in 6–7 GB, on your GPU or your CPU

[Ternary-Bonsai-2-27B](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) (by **PrismML**,
Apache-2.0, base model Qwen/Qwen3.8-27B, [whitepaper](https://github.com/PrismML-Eng/Bonsai-demo/blob/main/bonsai-2-27b-whitepaper.pdf))
is a 27-billion-parameter thinking model quantized to **ternary** weights. It ships in two GGUF
formats, both the same model:

| file | size | bits/weight | pick it when |
|---|---|---|---|
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | 7.2 GB | ~2.1 | a CUDA GPU with 10 GB+ VRAM, or Apple silicon with 16 GB+ — the fast kernels (~130–143 tok/s on an RTX 5090, PrismML's figure) |
| `Ternary-Bonsai-2-27B-PTQ1_0.gguf` | 5.9 GB | 1.75 | CPU, Vulkan, smaller GPUs — the smallest file |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | 629 MB | — | vision: pass it as `--mmproj` |

Either way the box needs **12 GB+ RAM**. The previous generation, [bonsai-27b](bonsai-27b.md)
(Q1_0, 3.8 GB), still works and still runs in 8 GB; this one is the better model.

**The catch, and it is a sharper one than Bonsai 1's:** the weights carry a Walsh-Hadamard
rotation (`prism.hadamard.*` metadata in the GGUF) that the runtime has to undo on the
activations. **Stock llama.cpp and Ollama load the file without a single warning and emit
gibberish.** Only PrismML's [llama.cpp fork](https://github.com/PrismML-Eng/llama.cpp) at release
`prism-b10685-7dffb15` or newer does it right (PRs #148 and #150 on the `prism` branch). "It
loaded" is not evidence — ask it a question.

## Set it up — prebuilt binary (no compiler)

The fork publishes binaries for Linux (cpu / vulkan / cuda-12.4 / rocm), macOS (arm64 / x64),
Windows (cpu / vulkan / cuda) and Android. Names follow
`llama-<release>-bin-<os>-<backend>-<arch>`; check the release's asset list before trusting a
name.

```bash
RELEASE=prism-b10685-7dffb15
BASE=https://github.com/PrismML-Eng/llama.cpp/releases/download/$RELEASE
# pick ONE:
ASSET=llama-$RELEASE-bin-linux-cuda-12.4-x64.tar.gz     # NVIDIA, 10 GB+ VRAM -> PQ2_0
# ASSET=llama-$RELEASE-bin-ubuntu-vulkan-x64.tar.gz     # any GPU via Vulkan -> PTQ1_0
# ASSET=llama-$RELEASE-bin-ubuntu-x64.tar.gz            # CPU only          -> PTQ1_0
# ASSET=llama-$RELEASE-bin-macos-arm64.tar.gz           # Apple silicon     -> PQ2_0 at 16 GB+
mkdir -p ~/bonsai2/bin && curl -fL "$BASE/$ASSET" | tar -xz -C ~/bonsai2/bin
LLAMA=$(find ~/bonsai2/bin -name llama-server -type f | head -1)
"$LLAMA" --version    # must run before you download 6 GB of weights

# weights: our mirror first, HuggingFace as the fallback (same file name on both)
QUANT=PQ2_0           # or PTQ1_0
mkdir -p ~/bonsai2/models && cd ~/bonsai2/models
for f in Ternary-Bonsai-2-27B-$QUANT.gguf Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf; do
  curl -fL -C - -o "$f" "https://weights.aitherium.com/$f" \
    || curl -fL -C - -o "$f" "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/$f"
done

# serve: OpenAI-compatible on :8080, loopback only
"$LLAMA" -m ~/bonsai2/models/Ternary-Bonsai-2-27B-$QUANT.gguf \
  --mmproj ~/bonsai2/models/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf \
  --host 127.0.0.1 --port 8080 -c 16384 -ngl 99 -fa on \
  --temp 1.0 --top-p 0.95 --top-k 20 --reasoning-budget 2048 --alias bonsai-selfhost
```

Windows (PowerShell): same release, `llama-$RELEASE-bin-win-cuda-12.4-x64.zip` plus
`cudart-llama-bin-win-cuda-12.4-x64.zip` unpacked next to `llama-server.exe` (or
`-win-vulkan-x64.zip` / `-win-cpu-x64.zip`, which need no cudart).

The flags matter:

- `-fa on --temp 1.0 --top-p 0.95 --top-k 20` — PrismML's recommended serving defaults for the
  thinking model; set them server-side so bare clients inherit them.
- `--reasoning-budget 2048` — the chat template force-opens a `<think>` block every turn; with
  llama-server's default of `-1` it never closes and `message.content` comes back **empty**.
- `--host 127.0.0.1` — an unauthenticated inference server on `0.0.0.0` is published to your
  whole network.

### Or build the fork from source (CPU node, no prebuilt for your platform)

```bash
sudo apt-get update && sudo apt-get install -y build-essential cmake libcurl4-openssl-dev git
git clone https://github.com/PrismML-Eng/llama.cpp ~/llama.cpp
cd ~/llama.cpp && git fetch --tags && git checkout prism-b10685-7dffb15   # a TAG, not the branch tip
cmake -B build -DGGML_CUDA=OFF -DGGML_NATIVE=ON -DLLAMA_CURL=ON
cmake --build build -j"$(nproc)" --target llama-server
```

## Use it

```bash
curl http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "bonsai-selfhost",
  "messages": [{"role":"user","content":"Reply with the single word: ready"}],
  "max_tokens": 256
}'
```

Read the **last** `content` field — the response carries `reasoning_content` first. Any
OpenAI-compatible client works against `http://127.0.0.1:8080/v1`. For an image, send it the
usual way (`image_url` content part); the mmproj handles it.

## Keep it running

```ini
# ~/.config/systemd/user/bonsai2-llama.service
[Unit]
Description=Bonsai 2 27B llama-server (PrismML fork)
[Service]
ExecStart=%h/bonsai2/bin/llama-server -m %h/bonsai2/models/Ternary-Bonsai-2-27B-PQ2_0.gguf --mmproj %h/bonsai2/models/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf --host 127.0.0.1 --port 8080 -c 16384 -ngl 99 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --reasoning-budget 2048 --alias bonsai-selfhost
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now bonsai2-llama
```

## What to expect

- **Gibberish?** You are on stock llama.cpp, Ollama, or a fork build older than `prism-b10685`.
  Nothing else produces it on a good download. `llama-server --version` tells you which.
- **Empty answers?** `--reasoning-budget` is missing, or the budget is spent thinking; raise
  `max_tokens` and read the last `content` field.
- **Speed:** PQ2_0 on a 5090 is chat-fast; PTQ1_0 on a CPU is a few tokens a second — fine for
  background reasoning, not chat you watch type. Add `/no_think` to a prompt to skip thinking.
- **The one-click installer** at `https://aitherium.com/install-bonsai.sh` (and
  `install-bonsai.cmd` on Windows) does everything above without a question: it detects the
  GPU, picks the format and the backend, pins this release, fetches the mmproj on `--vision`,
  and registers autostart.

## Part of one substrate

A Bonsai 2 box is a reasoning [awnode](aithernode.md); join it to a fleet with
[AitherMesh](aithermesh.md), point [awdk](awdk.md) agents at its `:8080/v1` endpoint, and
provision the host with [AitherZero](aitherzero.md).

Model + fork © **PrismML** — <https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf>. This
skill is MIT-licensed, like everything in `awskills`.

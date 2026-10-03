# KV holder: lend a phone's memory to your model's context

A model's context window is capped by the memory left next to its weights. A **KV holder**
keeps the oldest part of that context on another device, a phone, a second laptop or a
server, and computes the attention over it. The machine running the model merges the
holder's share with its own. The merge is exact: the output equals attention over every key.

The holder speaks PATN v3, the protocol of [Backburner](https://github.com/StayLameBro/backburner)
(MIT). An `adk` host can use a Backburner iPhone, and a Backburner host can use an `adk` holder.

## Phones: no app to install

The machine running the model starts a relay. The phone opens a page in its browser and
dials out to it. Because the phone always connects outward, the same relay works over USB,
the LAN, a mesh overlay or the public internet.

| How the phone is reached | Command on the model's machine | WebGPU on the phone |
|---|---|---|
| USB cable (Android) | `adk kvholder phone` | yes (`localhost` is a secure context) |
| Public tunnel | `adk kvholder phone --via tunnel` | yes (https) |
| LAN or mesh IP | `adk kvholder phone --via lan [--host <mesh-ip>]` | no: plain http, the page uses the CPU |
| This machine's browser | `adk kvholder phone --via local` | yes |

**USB (Pixel and other Android phones).** Turn on USB debugging (Settings > System >
Developer options), plug in, accept the prompt, and check that `adb devices` lists the phone.
`adk kvholder phone` maps the phone's `localhost` to this machine (`adb reverse`) and opens
the holder page on the phone. Keep the tab open; the page holds a screen wake lock.

**Tunnel.** Needs `awtunnel` (`pip install awtunnel`) and `cloudflared`. The command prints an
`https://…` link; open it on the phone from anywhere.

**The token.** Every link carries a one-time token after `#t=`. The fragment never reaches a
server log. A holder without the token is refused. The KV cache is derived from your prompts,
so treat the link like a password and prefer USB or the tunnel to an open LAN.

Whether the phone's GPU is used depends on its browser: the page shows `webgpu…` or `cpu`.
Both are exact; the GPU is much faster.

## A Python holder (Pixel Linux terminal, a laptop, a server)

```bash
pip install awdk numpy
adk kvholder serve --connect ws://<model-host>:50063/holder --token <token>
```

A Python holder stores keys as `f32` by default: decode attention over 123k keys takes about
80 ms on one CPU, at 3.8x the bytes of the q8 rows it receives. `--store wire` keeps the rows
as received (most context per MB; about 300 ms for the same call).
`--store tq4` (TurboQuant-style: rotate, then 4 bits per value plus one norm per vector)
holds about 2x the context of the 8-bit rows per MB and 7.8x the context of `f32`. It is the
one approximate store: a planted needle is still retrieved at cosine 0.995, and each call
takes about 190 ms over 20,000 keys.

On the model's machine use `adk kvholder phone --via lan` (or `--via tunnel` with a `wss://`
URL) so the holder can reach it. `adk kvholder serve` without `--connect` listens on
`127.0.0.1:50062` instead; PATN has no authentication, so bind a LAN address only on a
network you trust.

## Elastic: add holders on demand

One relay takes any number of holders. Each gets a range of key positions sized from the
memory it lends; every attention call goes to the holders that hold keys, in parallel, and
the relay merges their partials exactly. A holder that joins mid-session adds capacity at
once. A holder that drops reconnects without losing its keys. If it comes back empty, every
call fails loudly until the engine starts over; it never gets a partial answer.

```bash
adk kvholder phone --via tunnel              # the relay, reachable from anywhere
adk kvholder elastic --count 4 --minutes 60 --max-mb 8192
```

`elastic` mints one **join token** per holder. A join token works once and expires; on first
use the holder gets a session token for reconnects, and the relay's master token never leaves
the machine. With [awrun](https://github.com/Aitherium/awrun) installed, each holder is an
awrun `ci` run of a `workflow_dispatch` workflow in your repo (`--workflow`, inputs `relay`,
`join`, `minutes`, `max_mb`) that runs:

```bash
adk kvholder serve --connect "$RELAY" --token "$JOIN" --max-mb "$MAX_MB"
```

Without awrun it calls `gh workflow run`. `--print-only` mints the tokens and prints that
command for any other machine: a container, a VM, a laptop on the mesh.

Any deployer that can start a container can add a holder. The container needs only outbound
https to the relay:

```bash
docker run --rm -e RELAY=wss://<relay>/holder -e JOIN=<join token> python:3.11-slim \
  sh -c 'pip install -q awdk numpy && exec adk kvholder serve --connect "$RELAY" --token "$JOIN" --max-mb 4096'
```

## Plan and check

```bash
adk kvholder plan --free-gb 8                # how many old tokens 8 GB holds (Qwen3.8-27B / Bonsai 2, q8_0)
adk kvholder relay-status                    # is a holder attached
adk kvholder probe 127.0.0.1:50062           # HELLO, link latency, holder stats
```

## The engine side

The engine connects to the relay's PATN port `127.0.0.1:50062`. Backburner's llama.cpp fork
does this on Apple silicon (`LLAMA_KV_REMOTE=127.0.0.1:50062`); the CUDA port of the same
hook is in progress. The holder is exact at the operation level on NVIDIA. A full-model run
through it has not been measured yet.

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

**Model shapes.** The page speaks PATN v4 shapes as well as v3: key and value widths from 32
to 4096 (multiples of 32, and they may differ), any number of query rows per KV head that is a
multiple of 8, up to 16 KV heads. So a phone holds context for Qwen3 (128 wide, 16 rows),
Llama and Gemma layouts, and DeepSeek MLA (keys 576, values 512, 128 rows), not only the
256-wide Qwen3.8 shape. It refuses a CONFIG with the same messages as the Python holder.
The WebGPU engine compiles one shader per shape when the host configures it. Add
`&store=tq4` to the link for about 4x the keys per MB (approximate); tq4 needs power-of-two
widths, so an MLA host is refused on a tq4 page. Measured in headed Chromium on an RTX 5090,
one layer, 6 calls (2026-10-03):

| shape (k / v / rows x KV heads) | f16 store, max abs error | ms per call (8192 q8_0 keys) |
|---|---|---|
| 128 / 128 / 16 x 2 (Qwen3) | 5e-05 | 4.6 |
| 64 / 128 / 8 x 2 | 7e-05 | 3.9 |
| 256 / 256 / 48 x 2 (v3) | 7e-05 | 3.8 |
| 576 / 512 / 128 x 1 (MLA) | 8e-05 | 8.9 |
| 128 / 128 / 64 x 8 (Llama 70B) | 1.1e-04 | 8.7 |

The tq4 store's relative error was 0.13-0.14 on every power-of-two shape, the same as the
Python `--store tq4` holder on the same data.

**What the page shows.** The state says what the phone is doing, from its own counters:
*Idle* (connected, no engine has sent context), *Ready* (in the pool, no keys reached it
yet), *Holding* (keeps keys, no attention call in the last 5 s) or *Active* (calls/s).
Below it: lent memory used of the amount lent, keys held per layer, attention calls per
second over the last minute, last and average ms, the engine (`webgpu-f16 <adapter>` or
`cpu`), the wake lock, and battery and CPU pressure when the browser exposes them. One tap
changes the amount lent (a change that would drop held keys asks twice); **Stop lending**
closes the link and frees the memory.

## Several phones: the swarm view

The relay takes any number of holders. Each gets a contiguous range of key positions sized
by the memory it lends; an attention call goes to every holder that keeps keys, in parallel,
and the partial answers are merged exactly. On the model's machine open
`http://127.0.0.1:<web-port>/swarm#m=<owner token>` (the `t=` value in the link the relay
printed). It shows the pooled memory, each holder's key range, memory used, last / mean /
round-trip ms and health, and **Add another phone**: a single-use join link (15 min) with a
QR code for each way a phone can reach the relay (tunnel, LAN, USB).

`/swarm` answers only requests from this machine addressed to `127.0.0.1` or `localhost`;
minting a link also needs the owner token. `/status` (JSON) carries the same per-holder
numbers (`held`, `used_bytes`, `calls`, `last_ms`, `mean_ms`, `rtt_ms`, `seen_s`) and never a
token.

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
takes about 190 ms over 20,000 keys. On a GPU the same format costs nothing extra: open the phone link with
`&store=tq4` added after the token and the WebGPU holder unpacks the 4-bit codes inside its
shader, answering in 4 to 6 ms over 70,000 keys on a desktop GPU.

A tq4 holder **centers keys** by default. A real model's keys share a large per-head offset
(Qwen3-0.6B's layer 0), and a 4-bit code cannot hold that offset next to the detail. The
holder subtracts each head's mean key, fixed by the first append to that layer, before
encoding. It then adds `scale * q . mean` back to the lse it returns. The softmax is
unchanged by a per-query constant, so the merge stays exact. Measured on real Qwen3-0.6B KV
through the holder (last 512 tokens of a 2,048-token document): perplexity 10.27 with
`f32`, 10.71 with centered tq4 and 35.96 uncentered. A tq4 holder announces this in its
hello (`"tq4": "centered"`). The relay's `/status` lists a tq4 holder that does not under
`warnings` as "tq4 uncentered: approximate, known-bad on real models".

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

## Shared pool: sessions reuse a prefix already on the holders

Engine sessions that start from the same text (a system prompt, a repository, a document)
compute the same keys for it. With the pool (`adk.kvpool`) the first session publishes that
prefix as shared blocks and every later session attaches to them instead of sending it
again. Measured in the tests: two sessions over a 4,096-token prefix, the first sent
16.8 MB of APPEND rows, the second sent 0 bytes, through one holder or a relay with two.
Attention stayed exact against numpy for both sessions after each went its own way.

- **Keys.** A block is 256 positions. Its key is the model fingerprint, the holder's layer
  layout and store, and a hash chain over every token up to the block's end
  (`kvpool.chain_hashes(tokens, kvpool.model_fingerprint(...))`). A block key therefore
  names the whole prefix before it.
- **Copy-on-write.** Shared blocks are read-only. A session's own keys go after them;
  attention covers the shared blocks and the private tail in one pass, so the merge across
  holders stays exact. A `TRUNCATE` into the shared part copies the kept rows of that block
  into the session's private range and releases the rest.
- **Refcounts and eviction.** Each session holds one reference per block. A block nobody
  references stays resident for the next session until the holder needs room. Then the
  least recently released blocks go first, the deepest block of a chain before its head.
  A referenced block is never evicted: without room the `APPEND` is refused, as before.
- **Protocol.** Four message types after PATN's 13: `SESSION` (20) binds a connection or a
  relay link to a session, `POOL_ATTACH` (21) attaches the resident part of a prefix right
  after `CONFIG` and returns the token count, `POOL_PUBLISH` (22) offers the session's first
  blocks for sharing, and `POOL_STATUS` (23) returns JSON. A v3 or v4 holder answers
  "unknown message"; the engine helpers read that as "no pool" and the engine appends
  everything, as it always did. A connection that never sends `SESSION` gets the plain
  protocol: one state per holder that survives reconnects.
- **Relay.** One relay serves several engine sessions: it switches every holder link to the
  caller's session, and each holder gets the blocks inside its own key range. Sharing runs
  from position 0 and needs each holder's range to start on a block boundary; it stops at
  the first holder that does not qualify. A relay with a holder that has no pool refuses
  `SESSION`, so each engine keeps its own relay connection as before.
- **Stores.** `f32` and `tq4`. `wire` refuses the pool messages (`ERR`), and the engine
  falls back.

```bash
adk kvholder pool status                     # blocks, bytes, refcounts, sessions (relay or holder)
adk kvholder pool status --target 10.0.0.5:50062
```

Engine side, with any PATN client:

```python
from adk import kvpool
fp = kvpool.model_fingerprint("qwen3.8-27b", "q8_0")
hashes = kvpool.chain_hashes(prompt_tokens, fp)
kvpool.open_session(client, sid)          # False: no pool, keep one connection per context
client.configure(cfg)
start = kvpool.attach(client, fp, hashes) # tokens already resident: append from here
...                                       # append [start, n) as usual
kvpool.publish(client, fp, hashes)        # let the next session reuse it
```

**The platform directory.** AitherOS's Workspace Shared-Prefix Directory (Nexus,
`/kv-cache/register-prefix` and `/kv-cache/prefix-directory`) records which nodes hold a
prefix warm. `kvpool.NexusPrefixDirectory` is a thin client for it. Add it to a holder's
`pool.listeners` and every published block is registered under its chain hash. Every
evicted block is released. A router then sends a session to the relay that already holds
its prefix. The directory holds routing hints only; KV bytes never leave the holders. The
caller passes the credential header, and a directory that is down never fails the holder.

Not covered yet: an engine that disconnects without `BYE` leaves its session (and its
references) on the holders until a `CONFIG` on the same session id resets it. A holder
that joins the relay while a pooled session is loaded gets that session's `CONFIG` on
first use. It never receives shared blocks that were published before it joined.

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

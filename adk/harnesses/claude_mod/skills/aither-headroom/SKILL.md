---
name: aither-headroom
description: >-
  Cut agent token cost with reversible context compression. Agents burn most of their tokens re-sending bulky context every turn: verbose JSON tool output, retrieved documents, file dumps. [headroom](https://github.com/wizzense/headroom) (headroom-ai) crushes that content with a SmartCrusher pipeline — measured ~46% token savings on an 87 KB lint blob — while *protecting* conversation and user text so answers don't degrade. AitherOS wires it in two ways: an automatic pre-send hook at the single LLM chokepoint, and agent-callable tools you invoke mid-loop.
---

# aither-headroom — cut agent token cost with reversible context compression

Agents burn most of their tokens re-sending bulky context every turn: verbose JSON tool
output, retrieved documents, file dumps. [**headroom**](https://github.com/wizzense/headroom)
(`headroom-ai`) crushes that content with a SmartCrusher pipeline — **measured ~46% token
savings on an 87 KB lint blob** — while *protecting* conversation and user text so answers
don't degrade. AitherOS wires it in two ways: an **automatic** pre-send hook at the single
LLM chokepoint, and **agent-callable** tools you invoke mid-loop.

> Live-proven (headroom 0.25.0, 2026-07-19): a 26,853-token tool payload compressed to
> 14,471 tokens (`saved 12,382`, `router:smart_crusher:0.48`) with `router:protected:user_message`
> preserving the conversation. Plain chat text barely compresses — by design.

## How it's wired

- **Automatic (pre-send):** every LLM call funnels through `LLMGateway._post_generate`, which
  calls `CompressionClient` → the **headroom sidecar** (adoption mode A — a separate
  `aither-headroom` container, no in-process dependency). Flag-gated, graceful no-op: if it's
  disabled or the sidecar is unreachable/slow, the call proceeds **uncompressed**. Nothing breaks.
- **Agent-callable:** the free `headroom` tool pack adds two tools —
  `headroom_compress(content)` and `headroom_stats()` — so an agent can explicitly shrink a
  blob and see the real savings.

## Turn it on

The master switch defaults **OFF** (flip only after you've confirmed savings on your traffic).

```bash
# Per-process, no restart (env wins over config):
export AITHER_HEADROOM_ENABLED=true
# Or durably: set enabled: true in your deployment's config/headroom.yaml, then restart
# the service (mount it as config so a restart is enough — no rebuild).
```

The sidecar is plain HTTP on the internal network at `http://aither-headroom:8787`
(host-published at `http://127.0.0.1:8788`). Override with `AITHER_HEADROOM_URL`.

## Give an adk agent the tools (self-service)

The `headroom` pack is **free** (`tier: free`, `entitlements: []`) — apply it to your agent and
it gains `headroom_compress` / `headroom_stats`:

```
apply_pack_self("headroom")          # MCP tool — self-service, fail-closed, idempotent
# or discover first:
pack_list()                          # headroom shows tier:free, licensed:true
```

Agents routing through the AitherOS fleet gateway get the **automatic** compression for free
once it's enabled — the pack is the explicit, agent-driven path (and the way standalone / BYO
adk agents opt in).

## Call it directly

```jsonc
// Shrink a bulky blob before reasoning over it:
headroom_compress(content="<verbose JSON tool output / retrieved docs / file dump>")
// → {ok:true, compressed_content:"…", tokens_before, tokens_after, tokens_saved,
//    compression_ratio, transforms_applied}

// Is compression actually on?
headroom_stats()
// → {sidecar_url, healthy, headroom_version, presend_enabled, min_chars, hint}
```

## The one thing that decides whether you save anything: **JSON-wrap it**

SmartCrusher fires on **structured/JSON content**. Raw prose and raw code pass through
**completely untouched** — the role (`tool`/`assistant`), `tool_result` content blocks, and
`compress_user_messages` make **no difference**. Measured head-to-head on identical bytes
against the live sidecar:

| bulky context | sent as raw text | sent as JSON |
|---|---:|---:|
| RAG documents | **0.0%** | **86.6%** (4,821 → 645 tok) |
| code dump | **0.0%** | **98.3%** (7,219 → 121 tok) |
| file contents | **0.0%** | **75.3%** (6,254 → 1,545 tok) |
| JSON tool output | — | ~50% |

So when you stuff context into a message, **serialize it** — `{"docs": [...]}`,
`{"path": "contents", ...}` — instead of concatenating text. Conversation/user turns stay
protected either way, so accuracy is unaffected. Below ~800 chars it no-ops (the round-trip
isn't worth it).

> If you take one thing from this skill: raw-text context saves you **nothing**. The same
> content as JSON saves **50–98%**.

### ⚠️ But don't chase the big ratios — they're lossy in a way that matters

Compression this aggressive **decimates structurally-similar lines and keeps a sample**.
Needle-in-haystack test (unique canary buried in bulky content, then compressed):

| content | ratio | did the unique fact survive? |
|---|---:|---|
| diverse JSON records | 49.2% | ✅ canary intact |
| repetitive code dump | 97.1% | ❌ **`def handler_CANARY` dropped** |

In the code case the canary's *comment* survived but its `def` line didn't — producing
**mangled, misleading code**. The compressed output visibly skips: `handler_0, handler_1,
handler_22, handler_44, handler_66…`

**Rule of thumb:** ~50% on *diverse* structured records is safe. **90%+ means it threw away
unique entries.** Never route code — or any payload where every row matters — through
high-ratio compression without a needle check.

## Verify it live

```bash
# 1) Sidecar healthy?
curl http://127.0.0.1:8788/health          # → {"ok": true, "headroom": "0.25.0"}

# 2) Prove savings on real context (installs headroom-ai in an isolated venv, drives a
#    realistic coding-agent payload, reports token savings + transforms):
# measure it on a realistic payload before trusting the ratio

# 3) After enabling, watch LLMTraceStore for compression_ratio / tokens_saved per call.
```

## Part of one substrate

headroom is the efficiency wedge under the rest: stand up a box as an
[awnode](aithernode.md), run agents with [awdk](awdk.md), and every LLM call
your agents make gets cheaper without touching a single caller. Local compute is already `$0`;
this makes cloud calls smaller too.

MIT-licensed, like everything in `awskills`. headroom itself is `headroom-ai` on PyPI.

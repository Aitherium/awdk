# Aither-ADK — Agent Developer Guide

> How to build a real, production-grade agent (or a shippable **pack**) on `awdk`
> the right way the first time. This is the opinionated golden path — it encodes the
> mistakes you do **not** need to repeat.
>
> Companion to `README.md` (install/quickstart) and `GRID_SETUP.md` (distributed inference).

---

## 0. The one thing to internalize

**`AitherAgent.chat()` IS the agent.** It already does, in one call:

- a native function-calling **ReAct loop** that decides when it has enough and answers
  (no magic `FINAL:` token, no synthesis fallback to bolt on);
- **persistent conversation memory** (SQLite, keyed by `session_id`, survives restarts);
- **knowledge-graph recall** — injects relevant prior facts into every prompt;
- **knowledge-graph ingestion** — extracts entities/relations from each turn;
- **continual skill learning** — extracts a reusable skill from successful multi-tool runs;
- per-agent **token metering**.

> If you find yourself hand-rolling a tool loop, a "did it answer? if not, synthesize"
> fallback, or your own conversation history list — **stop**. `chat()` already does it.
> Reinventing these is the #1 way to ship a worse agent than the SDK gives you for free.

```python
from adk import AitherAgent
agent = AitherAgent("aither")            # identity + tools + memory + graph + meter
resp = await agent.chat("Research X and summarize", session_id="user-42")
print(resp.content, resp.tokens_used, resp.tool_calls_made)
```

`stream_react()` exists for live `<think>`/token streaming, but it relies on a **text**
ACTION/FINAL protocol that weaker models over-run (they keep searching and never emit
`FINAL`). Prefer `chat()` (native tool calls) for reliability; stream tool events by
wrapping `agent._tools.execute` (see §6) if you need a live UI.

---

## 1. BYO inference (no signup, no SaaS)

`LLMRouter` talks to any backend with the user's own key — zero Aitherium account needed.

```python
from adk.llm import LLMRouter
llm = LLMRouter(provider="anthropic",  api_key="sk-ant-...")     # Claude
llm = LLMRouter(provider="openai",     api_key="sk-...")         # OpenAI
llm = LLMRouter(provider="deepseek",   api_key="sk-...")         # DeepSeek (OpenAI-compat)
llm = LLMRouter(provider="ollama")                                # local, no key
agent = AitherAgent("aither", llm=llm)
```

Providers handled natively: `anthropic`, `openai`, `deepseek`, `groq`, `together`,
`vllm`, `lmstudio`, `llamacpp`, `ollama`, `gateway`, `picolm`. The OpenAI-compatible ones
get the correct `base_url` + default model automatically.

### ✅ Self-hosted / BYO-key is uncapped by default

You do **not** need to disable anything. `AitherAgent` caps tokens and reasoning
effort on **exactly one** backend — Aitherium's metered cloud gateway
(`provider="gateway"`), where Aitherium pays for the inference. Every other backend
you wire up here — `anthropic` / `openai` / `deepseek` / `groq` BYO keys, local
`ollama` / `vllm` / `llamacpp` / `lmstudio`, or any custom OpenAI-compatible
endpoint — is **your** inference and is never capped or effort-limited. Enforcement
is opt-in by provenance and **fail-open**: an unrecognized backend is treated as
self-hosted (uncapped), never bricked.

```python
from adk.llm import LLMRouter
agent = AitherAgent("aither", llm=LLMRouter(provider="deepseek", api_key="sk-..."))
# No env vars, no quota surgery. Full effort (7-10), no monthly token cap.
```

Mechanics (so you can audit it): `AitherAgent.__init__` sets `self._metered_gateway =
is_metered_backend(llm.provider_name)`; the monthly cap is applied and the effort
clamp runs **only** when that is true, and `adk.metering.AgentMeter.can_spend()`
fail-opens (returns `ALLOW`) for any non-gateway backend regardless of what limit a
quota carries.

If you ever want to force-disable **all** license enforcement explicitly (e.g. you run
the metered gateway under your own contract), the global override still exists:

```python
import os
os.environ["AITHER_LICENSE_ENFORCE"] = "0"   # set BEFORE constructing the agent
```

---

## 2. Author a **pack**, don't write a framework

A pack is a directory the ADK auto-discovers (`adk.pack_discovery`) and applies. It is the
unit of reuse and the thing you ship.

```
my-pack/
  brain_pack.yaml     # persona / system_prompt, UI labels, doc_types, safety, tool whitelist
  agent.yaml          # name, brain_pack, capabilities, enabled_domains
  skills/*.md         # methodology the agent follows (folded into the system prompt)
  packs/*.yaml        # (optional) tool-pack manifests
```

Discovery order (`discover_brain_pack()` / `discover_agent_yaml()` / `discover_pack_dir()`):
`AGENT_BRAIN_PACK` env → CWD → `Library/packs/` → entry point `aither.brain_packs` →
`~/.aither/packs/<name>/` → bundled fallback.

Ship it as a pip package with an entry point, or just drop the dir in `~/.aither/packs/`:

```toml
# pyproject.toml of your pack
[project.entry-points."aither.brain_packs"]
my-pack = "my_pack:get_pack_dir"
```

Apply a pack to a base agent by reading `brain_pack.yaml`'s `system_prompt` (+ `skills/*.md`)
and passing it as the agent's identity/system prompt:

```python
import yaml
from adk.pack_discovery import discover_brain_pack
from adk.identity import Identity
bp = yaml.safe_load(discover_brain_pack().read_text())
sysp = bp["system_prompt"] + "\n\n" + "\n\n".join(p.read_text() for p in skills_dir.glob("*.md"))
agent = AitherAgent("researcher",
                    identity=Identity(name="researcher", system_prompt=sysp),
                    system_prompt=sysp, builtin_tools=False)   # default-deny tools
```

---

## 3. Memory model — and how to *never forget*

Two tiers, by design:

| Tier | Backing | Scope | Recall |
|---|---|---|---|
| **Short-term** | `adk.memory.Memory` (SQLite) | last ~20 messages per `session_id` | verbatim window |
| **Long-term** | `adk.graph_memory.GraphMemory` (SQLite) | **all** entities/facts, persists across restarts | keyword + embedding hybrid |

`chat()` auto-loads the short-term window and injects the **top-3** graph hits (truncated to
200 chars). That is enough for normal chat — but under a long, noisy session it can surface
*partial* memory, and a weak model will then **fabricate** the missing fields. Embeddings
fall back to feature-hashing when Ollama isn't present, which weakens semantic ranking.

**To make recall reliable and hallucination-free (RAG grounding — this is NOT a fallback,
it's how an agent uses long-term memory):**

```python
nodes = await agent._graph.search(question, limit=8)       # retrieve MORE, untruncated
facts = [n.content for n in nodes if n.content]
grounded = (
    "Facts from YOUR memory (authoritative). Answer from these. If a detail is NOT "
    "present below, say you don't have it on record — NEVER invent names/numbers/dates.\n"
    + "\n".join(f"- {f}" for f in facts) + f"\n\nUser: {question}"
)
resp = await agent.chat(grounded, session_id=sid)
```

Pair it with the same rule in your `brain_pack.yaml` system prompt. Proven result: a planted
fact survives 25+ noise writes, an 11-turn session that pushes it out of the short-term
window, **and a full process restart** — recalled exactly, no web re-search, no fabrication.

Store facts with the model's own tool (`save_finding`-style → `graph.add_node`) or let
`chat()` ingest them automatically. The graph is at `$AITHER_DATA_DIR/graph/<agent>.db`;
set `AITHER_DATA_DIR` to control where memory lives (and to ship it self-contained).

---

## 4. Tools

Register any function; the registry builds the JSON schema from type hints + docstring.

```python
def web_search(query: str, limit: int = 6) -> str:
    """Search the web.  query: what to search for.  limit: max results."""
    ...
agent._tools.register(web_search)
```

- **Default-deny:** with `builtin_tools=False` the agent has *no* tools until you register
  them. Keep a whitelist in `brain_pack.yaml` and prune anything else — never ship an agent
  with `shell_exec`/`file_write` it doesn't need.
- **🚨 Coerce LLM-supplied args.** Models pass ints as lists/strings (`angles=["a","b"]`).
  Never `int(x)` a raw arg — coerce defensively, or the tool crashes mid-loop:
  ```python
  def _as_int(v, default):
      if isinstance(v, (list, tuple, set)): return len(v) or default
      try: return int(v)
      except (TypeError, ValueError): return default
  ```
- Built-in tools (`adk.builtin_tools`) include keyless DuckDuckGo `web_search`/`web_fetch`,
  file/git/code/python — registered per identity. Use `register_builtin_tools(agent, ["self"])`
  to add the honest `self_*` introspection tools ("what did I just do?") without the rest.

---

## 5. Token accounting (used vs. saved)

`LLMResponse` carries `prompt_tokens`/`completion_tokens`/`cache_status`. **Streaming returns
no usage** — so when you use `chat_stream`/`stream_react`, count the exact context + output
yourself with a tokenizer (tiktoken `cl100k_base`); it's a faithful measurement, not a guess.
Subclass `LLMRouter` to meter every call in one place:

```python
class LedgerRouter(LLMRouter):
    def __init__(self, *a, ledger, **k): super().__init__(*a, **k); self._ledger = ledger
    async def chat(self, m, **k):
        r = await super().chat(m, **k); self._ledger.record(r); return r
```

"Tokens saved by memory" = the token size of facts reused from the graph instead of
re-fetched + re-fed. Credit it when you RAG-ground (§3). Keep it honest — every number must
trace to a real counter (no invented savings).

---

## 6. Serving a chat UI (FastAPI)

Use `adk-serve` / `adk-serve --workspace` for the batteries-included server, or wrap your own.
To stream live tool activity while using the robust `chat()` loop, wrap `execute`:

```python
orig = agent._tools.execute
async def traced(name, args):
    emit({"type": "tool", "name": name, "args": args})
    out = await orig(name, args)
    emit({"type": "tool_result", "name": name, "result": str(out)[:1500]})
    return out
agent._tools.execute = traced
try:    resp = await agent.chat(message, session_id=sid)
finally: agent._tools.execute = orig
```

### 🚨 FastAPI + `from __future__ import annotations`

If your handler is `async def route(request: Request)` and `Request` is imported **locally**
(inside a factory function), FastAPI can't resolve the stringified annotation against module
globals → it treats `request` as a required **query param** → every call 422s
(`"Field required"`). **Fix: import FastAPI symbols at module level.**

---

## 7. The gotcha checklist (paste this above your desk)

| Symptom | Cause | Fix |
|---|---|---|
| `"reached your monthly token limit"` on BYO key | caps now apply only to `provider="gateway"` | nothing to set — BYO/local/custom backends are uncapped by default (upgrade ADK if you still see this) |
| Agent over-searches, returns empty answer | `stream_react` text protocol never emits `FINAL` | use `agent.chat()` (native tool calling) |
| Agent invents names/numbers on recall | partial top-3 graph injection under noise | RAG-ground top-8 + "don't invent" rule (§3) |
| Tool crashes `int() ... not 'list'` | model passed a list/str for a number | coerce args (§4) |
| FastAPI route 422 `Field required: request` | `__future__ annotations` + local `Request` import | import FastAPI at module level (§6) |
| Token meter reads 0 while streaming | providers omit usage on stream | tokenizer-count the wire text (§5) |
| Memory "forgotten" after 10+ turns | only in short-term window (20 msgs) | it's in the **graph** — RAG-recall it (§3) |
| Custom identity blocked | `AITHER_LICENSE_ENFORCE=1` set on a self-hosted box | unset it — identity gating is opt-in (off unless you turn it on) |
| Effort silently capped at 3 on BYO key | (fixed) effort clamp was global; now gateway-only | upgrade ADK; self-hosted keeps full effort 7-10 |

---

## 8. Worked example

`adk/templates/deep-research/` — shipped inside the package, so it is already on your disk
after `pip install awdk` — is a complete, verified reference: a sign-in-free
deep-research analyst pack (BYO Anthropic/OpenAI/DeepSeek/Ollama key) that searches → reads
→ cites → writes PDF/DOCX/MD reports, with a live token used/saved meter. It demonstrates
every section here: the pack layout (§2), RAG-grounded never-forget memory (§3, proven
across a restart), default-deny tools with arg coercion (§4), the `LedgerRouter` (§5), and
the `chat()` + traced-execute server (§6).

Read these two as the canonical template:

- `adk/templates/deep-research/serve.py`
- `adk/templates/deep-research/pack/deep-research/brain_pack.yaml`

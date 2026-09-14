# GobboNet on aither-adk

Run [GobboNet](https://github.com/ElodineOfficial/GobboNet) (Elodine / GoblinCorps, MIT)
with an agent engine behind it — keyless web search, conversation persistence, and model
weights without a HuggingFace account — entirely on your own machine.

**GobboNet is not modified and not redistributed.** You clone it yourself. This pack is the
engine; their UI, character cards and extension seams stay exactly as they are.

**It also runs on macOS and Linux.** GobboNet ships `launch.bat`, `fileserver.ps1` and
three more PowerShell scripts, so the app is Windows-bound today — not because the UI is,
but because five scripts are. This pack replaces them: `server.py` covers the launcher and
the file server, and `models.py` serves the four endpoints behind the model picker
(`/models-list.json`, `/active-model.json`, `/swap-model`, `/swap-status`). The UI is
unchanged and does not know the difference.

---

## Set it up (three commands, no account)

```bash
pip install aither-adk
git clone https://github.com/ElodineOfficial/GobboNet
python -m adk.packs.gobbonet.server --ui ./GobboNet --port 11434
```

Open `http://127.0.0.1:11434/chat.html`.

Then in GobboNet's settings, point search at the same server:

```
SEARCH_URL=http://127.0.0.1:11434/web_search
```

That is the whole setup. No sign-up, no API key, no hosted service — including ours.
Bound to loopback by default; local-first is the point, not a setting.

Check the install with `adk doctor`.

---

## Why this exists

GobboNet's maintainer has been specific about where it stops:

> *"Can not: be a complex agent … run an intense harness … reliably produce code outside
> context limits [most agents have special sauce to handle this]"*

And its front page promises **"No account, no sign-up, no email"** — while web search asks
for an Ollama account and an API key.

adk supplies the engine half. The parts that matter:

### Web search with no account

adk ships keyless DuckDuckGo search through a maintained client, and the pack serves it on
the contract GobboNet already speaks —
`POST {"query","max_results"}` → `{"results":[{title,url,content}]}`.

Worth being blunt about why this needs a package at all: **there is no keyless search
endpoint that answers a bare HTTP client.** Measured — `searx.be?format=json` returns HTML
(public instances disable the JSON API), Mojeek 403s, Marginalia 302s, and DuckDuckGo's
HTML endpoint answers a plain client with a bot interstitial containing no results. Every
working option is a maintained Python or Node client. That is exactly why GobboNet reached
for Ollama's keyed search, and why *installing something that has a maintained client* is
the fix rather than writing another scraper.

We had that lesson the expensive way: our own scraper kept returning 3/3 results with
**empty** snippets after DuckDuckGo changed their markup — titles and URLs still parsed, so
nothing failed. A stale scraper does not break, it **thins**, and the model reads the result
as "the web had nothing useful."

### Conversation persistence

GobboNet's `/state` endpoint uses raw `fetch`, so a browser-side shim structurally cannot
serve it — which is why an unserved GobboNet shows `sync error: HTTP 404` and saves nothing.
This is a real HTTP server, so `/state` round-trips to a JSON file beside your UI folder.

### Semantic retrieval

`/v1/embeddings` is the semantic half of GobboNet's hybrid retriever. Without it, upstream
falls back to tag matching **silently** — quality drops and nothing says so. Wire an
embeddings model through a custom `Engine` (below) and that half comes back.

### Model weights without HuggingFace

```python
from adk.models import mirror, fit
from adk.hardware_probe import detect_system

for m in fit.fit_models(detect_system(), mirror.CATALOG):
    print(m.classification, m.model_name, "-", m.reason)

mirror.MirrorClient().download("gemma4-12b-Q4_K_M.gguf", dest="./models")
```

Tells you what your machine can actually run in plain language, then pulls it: resumable
(a 46 GB download that restarts from zero on a dropped connection is not usable),
rate-capped by default so it cannot saturate a home link, and size-verified on completion —
a truncated GGUF otherwise fails at load time with an error far from the cause.

### The model picker, off Windows

The dropdown at the top of GobboNet's UI is served by `fileserver.ps1` upstream. This pack
serves the same four endpoints from `models.py`, so the picker works on any OS:

```bash
adk gobbonet --setup-model          # installs llama.cpp + a GGUF that fits this machine
python -m adk.packs.gobbonet.server --ui ./GobboNet
```

GGUFs in `~/.aither/models` appear in the dropdown. Selecting one restarts llama.cpp on it
and the UI polls until it is ready.

Two details worth knowing, because both are how this class of thing usually breaks:

- **Ready means answering, not started.** A loading model accepts the socket long before it
  can answer a prompt. The swap reports `ready` only once a real request succeeds, so your
  first message does not hang with no explanation.
- **Sharded weights appear once.** `…-00001-of-00003.gguf` is listed; the other shards are
  not, because selecting one would load a fragment.

---

## What the pack adds behind the chat box

GobboNet speaks the OpenAI-compatible chat API, which is the seam: the pack
answers `/v1/chat/completions` by running adk's ReAct loop and streaming the
result back as ordinary assistant tokens. **The UI needs no changes** — it just
gets a much more capable model.

Behind it: 20 tool categories (code, file_io, git, graph, notebooks, persona,
python, secrets, shell, swarm, voice, web, workspace, …), graph-RAG retrieval,
agent notebooks, aeon, Strata, Lockbox, AitherShell, and the node/MCP surface.

```bash
adk gobbonet --setup-model    # install llama.cpp + a model sized to your machine
adk gobbonet --backend URL    # or point at ollama / vLLM / LM Studio
adk gobbonet --plain          # raw model, no tool loop
```

Backends are discovered, not configured — llama.cpp, ollama, vLLM and LM Studio
all speak the same API.

### Its own knowledge ledger

The pack keeps an awgit oplog of what it learns and merges it into a prime log.
Git's model applied to knowledge: every agent works locally, publishes, and
merges, so `contributors(node)` can say **which agent** decided something —
a question a single shared log cannot answer. Conflicts are reported, never
auto-resolved.

Because awgit keys changes on stable symbol identity, moving a function does not
erase what the graph knew about it. A file-based indexer sees one file shrink
and another grow, discards the old node, and loses its history at the exact
moment that is most confusing.

### Optional: linking to an account

Everything above runs with no account. If you *want* secrets on two machines,
session sync, or the Awconnect browser extension, that plane exists and is
strictly opt-in:

```bash
adk login              # browser device flow
adk secret sync        # bidirectional, vault-backed
adk deploy connect     # Awconnect browser extension
```

Nothing is initiated for you and nothing leaves the machine until you run one of
these. GobboNet's promise is that it runs on your machine; a pack that quietly
started syncing would take away the reason to use it.

## Wiring your own model and tools

The server takes an `Engine`. Every method may raise `NotConfigured`, which becomes a 503
**with a reason** — never an empty result. That distinction is deliberate: "not configured"
and "found nothing" must not look alike, or a broken feature impersonates a working one.

```python
from pathlib import Path
from adk.packs.gobbonet.server import Engine, serve

class MyEngine(Engine):
    def stream_chat(self, messages, **opts):
        for token in my_local_model(messages, **opts):
            yield token

    def embed(self, texts):
        return my_embedder(texts)

serve(Path("./GobboNet"), MyEngine(), port=11434).serve_forever()
```

A custom `Engine` replaces the built-in one entirely, so anything you do not implement
refuses honestly — nothing is faked, and nothing silently falls back to a guess. Use this
when you want your own runtime; use `adk gobbonet` when you want the agent loop above.

---

## Shipping your own app this way

GobboNet is a **pack**, and packs are how adk distributes an application. A pack is a
directory holding `brain_pack.yaml` (persona, features, UI labels, safety) and `agent.yaml`
(capabilities, panels, enabled domains), optionally with `skills/` and tool-pack manifests.

adk finds packs five ways, in priority order:

| # | source | how |
|---|---|---|
| 1 | `AGENT_BRAIN_PACK` | explicit path — always wins |
| 2 | current directory | a `brain_pack.yaml` where you are |
| 3 | entry points | `[project.entry-points."aither.brain_packs"]` in a pip package |
| 4 | `~/.aither/packs/<name>/` | drop a directory in, no packaging |
| 5 | bundled | packs shipped inside the adk wheel (this one) |

So a third party ships an app either by dropping a folder in `~/.aither/packs/`, or by
publishing a pip package that registers an entry point:

```toml
[project.entry-points."aither.brain_packs"]
myapp = "aither_pack_myapp:get_pack_dir"
```

Then `pip install aither-adk aither-pack-myapp` and the pack is discovered automatically.
`adk pack list` shows what is available; `list_available_packs()` is the same view in Python.

---

## Notes

- **Your model, your choice.** This pack neither selects nor bundles one.
- **`max_tokens: -1`** means "no limit" to several clients; forwarded literally it asks for
  minus-one tokens and yields an empty completion with a clean `[DONE]`, which reads as a
  broken model. The server drops it.
- **Small models are the target.** The pack's prompt assumes 1–8B and tells the agent to keep
  a re-derivable working set rather than a transcript, because the failure mode of a small
  model on a long task is confident drift.
- **Licensing.** GobboNet is MIT (Elodine / GoblinCorps). Cloned by you, unmodified by us.

# GobboPack

`gobbonet` · version `3.3.0` · 14.8 KB

**[Download gobbonet-3.3.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/gobbonet-3.3.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/gobbonet-3.3.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/gobbonet-3.3.0.tar.gz
tar xzf gobbonet-3.3.0.tar.gz
python gobbonet/install.py
```

Installs to `~/.aither/packs/gobbonet/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

GobboNet (Elodine / GoblinCorps, MIT) is a local-first chat client: pick a
model, open a page, talk. Nothing leaves the machine. Its maintainer has
publicly described what it is NOT good at, and the list is specific:

"Can not: be a complex agent ... run an intense harness ... reliably
produce code outside context limits [most agents have special sauce to
handle this]"
"Will soon be able to: produce more complex file structures [spreadsheets,
slides, etc] ... group chat where multiple AIs speak to the other ... read
text it generates back to you"

This pack is that harness, offered so those stay strengths of GobboNet rather
than a rewrite of it. The "special sauce" is not a secret: it is a turn loop
that keeps a working set small and re-derivable, so a long task does not
depend on everything fitting in one window.

THE CONSTRAINT THAT SHAPES EVERYTHING BELOW: GobboNet's value is that it runs
on YOUR machine with no account. An integration that quietly needs a hosted
service would take the product's whole point away. So this pack is built to
run against the user's OWN local model, and every capability that genuinely
cannot be local is marked optional and degrades to a clear message rather
than a broken feature.

## Contents

```
README.md
agent.yaml
brain_pack.yaml
launch.py
server.py
```

---

# GobboNet on aither-adk

Run [GobboNet](https://github.com/ElodineOfficial/GobboNet) (Elodine / GoblinCorps, MIT)
with an agent engine behind it — keyless web search, conversation persistence, and model
weights without a HuggingFace account — entirely on your own machine.

**GobboNet is not modified and not redistributed.** You clone it yourself. This pack is the
engine; their UI, character cards and extension seams stay exactly as they are.

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

---

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

Only `web_search` is wired by default. Anything you do not implement refuses honestly —
nothing is faked, and nothing silently falls back to a guess.

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

---

sha256 `173ae1c0f25601f5b4d8c5d04e748da8bde24cabb12187f45e7b09175c17df2c`  
Built from `v3.3.0` (adk 3.3.0). [All packs](../packs.md)

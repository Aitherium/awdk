"""Find a local model server, or tell the user exactly how to get one.

The pack served a UI and keyless search and then refused to chat, which is a
strange thing to hand someone: a chat client with no model behind it. Everything
needed to fix that already shipped in adk — `llamacpp_setup` installs llama.cpp
and a fitting GGUF, `ollama_setup` drives ollama, `adk.models.fit` says what the
machine can actually run, and `adk.models.mirror` fetches weights without a
HuggingFace account. None of it was wired to the pack.

This connects them. Everything here speaks the OpenAI-compatible API, which is
the one thing llama.cpp, ollama, vLLM, LM Studio and GobboNet itself all agree
on, so "support a backend" means "know its port" rather than "write an adapter".

TWO TRAPS THIS HANDLES, both of which look like the model being broken:

1. **Do not proxy to yourself.** This pack's own server answers on GobboNet's
   default port (11434), which is also ollama's. If discovery finds "a server"
   on the port we are serving, forwarding to it is an infinite loop that
   presents as a hang, not an error. Our own port is excluded by construction.

2. **A reachable port is not a working model.** A server that is listening but
   has no model loaded answers `/v1/models` with an empty list and then errors
   on the first completion. Discovery requires a NAMED model, so "found a
   backend" means a backend that can actually answer.

When nothing is found the refusal names the command that fixes it. "Not
configured" with no next step is how a user concludes the tool is broken.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Optional

#: Ports worth checking, in the order a local-first user most likely has one.
#: llama.cpp first because that is what `adk gobbonet --setup-model` installs.
KNOWN_BACKENDS = [
    (8200, "llama.cpp (adk)"),
    (11434, "ollama"),
    (8000, "vLLM"),
    (1234, "LM Studio"),
    (5000, "text-generation-webui"),
]

TIMEOUT = 3


@dataclass
class Backend:
    """A local OpenAI-compatible server that has a model loaded."""

    url: str
    kind: str
    model: str

    def __str__(self) -> str:
        return f"{self.kind} at {self.url} (model: {self.model})"


def _model_names(data: dict) -> list:
    """Model ids from EITHER shape a real server answers with.

    OpenAI:    {"object": "list", "data": [{"id": ...}]}
    llama.cpp: {"models": [{"name": ..., "model": ...}]}

    Only the first was read, so a llama.cpp server -- measured, answering
    200 and completing chat -- was reported as 'listening but empty' and
    refused. On localhost too, not just when pinned."""
    if not isinstance(data, dict):
        return []
    out = [m.get("id") for m in (data.get("data") or [])
           if isinstance(m, dict) and m.get("id")]
    if out:
        return out
    for m in (data.get("models") or []):
        if not isinstance(m, dict):
            continue
        name = m.get("id") or m.get("name") or m.get("model")
        if name:
            out.append(name)
    return out


def _probe(target, kind: str) -> Optional[Backend]:
    """`target` is a full base URL, or an int port on localhost.

    It used to be a port ONLY, and built `http://127.0.0.1:{port}`
    unconditionally -- so a pinned `--backend http://other-host:8090` was
    silently rewritten to localhost and refused. discover() still passes
    ints, which is why both forms are accepted."""
    if isinstance(target, int):
        url = f"http://127.0.0.1:{target}"
    else:
        url = str(target).rstrip("/")
        # A pinned URL may already carry the API prefix; /v1/models is
        # appended below, so strip a trailing /v1 rather than probing
        # /v1/v1/models and calling a healthy server dead.
        if url.endswith("/v1"):
            url = url[:-3]
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    models = _model_names(data)
    if not models:
        # Listening but empty. Reporting this as a usable backend produces a
        # confusing failure one step later, on the first completion.
        return None
    return Backend(url=url, kind=kind, model=models[0])


def discover(exclude_port: Optional[int] = None) -> Optional[Backend]:
    """First local backend that is up AND has a model. None if there is none."""
    for port, kind in KNOWN_BACKENDS:
        if exclude_port is not None and port == exclude_port:
            continue  # never proxy to ourselves — that hangs rather than errors
        found = _probe(port, kind)
        if found:
            return found
    return None


def setup_hint() -> str:
    """What to actually run. A refusal without a next step reads as 'broken'."""
    return (
        "No local model server found.\n"
        "\n"
        "  adk gobbonet --setup-model      install llama.cpp + a model that fits\n"
        "\n"
        "Already running something? Point the pack at it:\n"
        "\n"
        "  adk gobbonet --backend http://127.0.0.1:11434    (ollama)\n"
        "  adk gobbonet --backend http://127.0.0.1:8000     (vLLM)\n"
        "\n"
        "Any OpenAI-compatible server works — that is the API GobboNet already speaks."
    )


def stream_completion(backend: Backend, messages: list[dict], **opts) -> Iterator[str]:
    """Proxy a chat turn, yielding token text as it arrives."""
    body = {
        "model": opts.get("model") or backend.model,
        "messages": messages,
        "stream": True,
    }
    for key in ("temperature", "top_p", "top_k", "max_tokens", "stop",
                "presence_penalty", "frequency_penalty", "seed"):
        if opts.get(key) is not None:
            body[key] = opts[key]

    # `max_tokens: -1` means "no limit" to several clients. Forwarded literally
    # it asks for minus-one tokens: the server returns an empty completion and a
    # clean [DONE], which reads as a broken model rather than a bad parameter.
    if body.get("max_tokens") is not None and int(body["max_tokens"]) < 0:
        body.pop("max_tokens")

    req = urllib.request.Request(
        f"{backend.url}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece


def embed(backend: Backend, texts: list[str], model: Optional[str] = None) -> list[list[float]]:
    """Embeddings, when the backend offers them.

    GobboNet's retriever is hybrid: without embeddings it falls back to tag
    matching SILENTLY, so quality drops with nothing reporting it. Raising here
    is deliberate — a caller that cannot embed should know, not quietly degrade.
    """
    req = urllib.request.Request(
        f"{backend.url}/v1/embeddings",
        data=json.dumps({"model": model or backend.model, "input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [row["embedding"] for row in data.get("data", [])]

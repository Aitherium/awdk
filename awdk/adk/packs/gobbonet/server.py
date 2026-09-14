"""Serve the GobboNet UI with an adk agent loop behind it.

GobboNet (Elodine / GoblinCorps, MIT) is a local-first chat client: a page, some
JS, and an OpenAI-compatible transport. It is deliberately simple, and its
maintainer has been explicit about where that simplicity stops -- it is not an
agent harness, it does not carry a task past the context window, and slides,
spreadsheets, speech and multi-agent chat are on the wishlist rather than in the
product.

This serves their UI unchanged and answers its endpoints from an agent loop, so
the front end people already like keeps working while the engine behind it does
more.

WHY A SERVER AND NOT A BROWSER SHIM. A shim that patches the page's fetch can
only intercept calls the page routes through its own hook. Three of GobboNet's
calls use RAW fetch and therefore cannot be intercepted at all:

    /state           conversation persistence  -> without it the UI shows
                                                  "sync error: HTTP 404"
    /v1/embeddings   the SEMANTIC half of its
                     hybrid retriever          -> returns null on failure and the
                                                  tag-based half silently carries
                                                  on, so retrieval quality drops
                                                  and NOTHING reports it
    /web_search      web search

Serving over real HTTP is what makes those answerable. That is the whole reason
this is a server: the two most valuable gaps are precisely the ones a shim
cannot reach, and the embeddings one is invisible when it breaks.

WHAT IS NOT NEGOTIABLE. GobboNet's promise is that it runs on your machine and
nothing leaves it. Every capability here is local-first, each optional one is
introduced by an explicit handler, and an unconfigured capability answers with a
clear message rather than silence or a fake. A wrong answer is worse than a
missing one -- especially for the endpoints below that degrade quietly.

    python -m adk.packs.gobbonet.server --ui /path/to/gobbonet --port 11434
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# ── The capability seam ──────────────────────────────────────────────────────
# Everything the engine can do arrives through this object. It is a plain
# protocol rather than an import of the agent runtime so that this module stays
# importable (and testable) without one, and so a host can substitute its own.
from adk.packs.gobbonet.agentic import AgenticEngineMixin  # noqa: E402
from adk.packs.gobbonet.models import ModelManager  # noqa: E402


class Engine:
    """What the UI needs from whatever is driving it.

    Every method may raise NotConfigured. The handler turns that into an honest
    message for the user; it must never be swallowed into an empty result, which
    is exactly how a broken retriever looks like a working one.
    """

    def stream_chat(self, messages: list[dict], **opts: Any) -> Iterator[str]:
        raise NotConfigured("chat")

    def models(self) -> list[dict]:
        raise NotConfigured("models")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotConfigured("embeddings")

    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        raise NotConfigured("web_search")

    def load_state(self) -> Optional[dict]:
        raise NotConfigured("state")

    def save_state(self, blob: dict) -> None:
        raise NotConfigured("state")


class NotConfigured(RuntimeError):
    """A capability the host did not wire up. Reported, never faked."""

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} is not configured on this server")
        self.what = what


class FileState:
    """Default persistence: a JSON file next to the UI.

    Deliberately the simplest thing that ends the "sync error: HTTP 404" the UI
    shows today. Local-first means the default has to work with no service.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> Optional[dict]:
        with self._lock:
            if not self._path.is_file():
                return None
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt blob must not masquerade as "no backup yet": that
                # would make the UI seed a fresh one over the user's history.
                raise

    def save(self, blob: dict) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(blob), encoding="utf-8")
            tmp.replace(self._path)  # atomic; a torn write loses the thread list


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    ui_root: Path
    #: Serves the four endpoints their `fileserver.ps1` owns. Optional so a
    #: caller can run the chat surface alone; the routes then 503 with a reason
    #: instead of 404ing, because "not found" reads as a version mismatch.
    models_mgr: Optional[ModelManager] = None

    server_version = "adk-gobbonet"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── routes ──────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == "/health":
            # Polled every few seconds; a non-200 puts the UI in its OFFLINE state.
            self._json({"status": "ok"})
            return

        if path == "/hub-manifest":
            # The Aither Hub contract (HUB-CONTRACT.md): this server is the
            # agentic GobboNet backend, so it declares the surfaces the hub
            # should merge while `adk up` serves it. The hub auto-registers
            # these while the node is present and degrades honestly on loss.
            self._json({
                "panels": [],
                "statusSources": [
                    {"id": "agentic", "label": "gobbonet agentic backend",
                     "degrade": "no local GobboNet agentic backend"},
                ],
                "rooms": [
                    {"id": "agentic", "name": "Agentic GobboNet",
                     "kind": "chat", "panelId": "chat"},
                ],
            })
            return

        if path in ("/v1/models", "/models"):
            try:
                self._json({"data": self.engine.models(), "object": "list"})
            except NotConfigured as exc:
                self._json({"error": str(exc)}, 503)
            return

        if path == "/state":
            try:
                blob = self.engine.load_state()
            except NotConfigured as exc:
                self._json({"error": str(exc)}, 503)
                return
            except Exception as exc:  # corrupt store — say so, do NOT seed over it
                self._json({"error": f"state unreadable: {exc}"}, 500)
                return
            if blob is None:
                # 404 here is the UI's "no backup yet" path; it then POSTs to seed.
                self._json({"error": "no state"}, 404)
            else:
                self._json(blob)
            return

        if path == "/info":
            # Sync manifest probe. 404 is EXPECTED upstream and means "no backup
            # yet", so answering it wrongly would suppress the seeding POST.
            self._json({"error": "no manifest"}, 404)
            return

        # ── the model picker, which upstream serves from fileserver.ps1 ──
        # Answering these is what makes the UI whole on macOS and Linux. The
        # response shapes are theirs (js/02-model.js), not ours.
        if path == "/models-list.json":
            mgr = self._models()
            self._json(mgr.models_list() if mgr else {"models": []})
            return

        if path == "/active-model.json":
            mgr = self._models()
            if mgr is None:
                # Their UI renders this string. "Unknown" is true and harmless;
                # a fabricated model name would be neither.
                self._json({"id": "custom", "name": "Unknown", "ggufFile": "",
                            "thinkingFormat": "none"})
                return
            self._json(mgr.active_model())
            return

        if path == "/swap-status":
            mgr = self._models()
            if mgr is None:
                self._json({"phase": "error",
                            "message": "model management is not enabled"})
                return
            self._json(mgr.swap_status())
            return

        self._serve_static(path)

    def _models(self) -> Optional[ModelManager]:
        return getattr(self, "models_mgr", None)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._body()

        if path.endswith("/v1/chat/completions"):
            self._stream_chat(body)
            return

        if path.endswith("/v1/embeddings"):
            # The SEMANTIC half of the hybrid retriever. Upstream treats a failure
            # as "no semantic hits" and quietly falls back to tag matching, so a
            # wrong answer here is invisible. Report the failure instead.
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not texts:
                self._json({"error": "input required"}, 400)
                return
            try:
                vectors = self.engine.embed(list(texts))
            except NotConfigured as exc:
                self._json({"error": str(exc)}, 503)
                return
            self._json({
                "object": "list",
                "data": [{"object": "embedding", "index": i, "embedding": v}
                         for i, v in enumerate(vectors)],
            })
            return

        if path.endswith("/web_search"):
            try:
                results = self.engine.web_search(
                    str(body.get("query") or ""),
                    int(body.get("max_results") or 5),
                )
            except NotConfigured as exc:
                self._json({"error": str(exc)}, 503)
                return
            self._json({"results": results})
            return

        if path == "/state":
            try:
                self.engine.save_state(body)
            except NotConfigured as exc:
                self._json({"error": str(exc)}, 503)
                return
            self._json({"ok": True})
            return

        if path == "/swap-model":
            mgr = self._models()
            if mgr is None:
                self._json({"message": "model management is not enabled"}, 503)
                return
            ok, message = mgr.swap(str(body.get("file") or ""))
            # `message` is the field their error path reads. A refusal that
            # answers 200 would be polled as a swap that never completes.
            self._json({"ok": ok, "message": message}, 200 if ok else 400)
            return

        self._json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        # `/jobs/<id>` cancellation ack. Jobs are declined below, so nothing here
        # ever exists; 404 is the honest answer and the UI handles it.
        self._json({"error": "not found"}, 404)

    def _stream_chat(self, body: dict) -> None:
        messages = body.get("messages") or []
        opts = {k: body[k] for k in ("temperature", "top_p", "max_tokens") if k in body}
        # max_tokens: -1 means "no limit" to several clients. Passed through as a
        # literal it becomes a request for minus-one tokens, and the stream ends
        # immediately with an empty completion that looks like a model failure.
        cap = opts.get("max_tokens")
        if cap is not None and (not isinstance(cap, int) or cap <= 0):
            opts.pop("max_tokens")

        try:
            chunks = self.engine.stream_chat(messages, **opts)
        except NotConfigured as exc:
            self._json({"error": str(exc)}, 503)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for text in chunks:
                self.wfile.write(_sse({"choices": [{"delta": {"content": text}}]}))
                self.wfile.flush()
            self.wfile.write(_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # the tab closed mid-stream; not an error worth logging

    def _serve_static(self, path: str) -> None:
        """Serve GobboNet's own files, unmodified."""
        rel = path.lstrip("/") or "chat.html"
        target = (self.ui_root / rel).resolve()
        try:
            target.relative_to(self.ui_root.resolve())
        except ValueError:
            self._json({"error": "not found"}, 404)  # path traversal
            return
        if not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _StateBackedEngine(Engine):
    """Wraps a host engine so persistence works even if the host ignored it.

    Persistence is the one capability that needs NOTHING external — a JSON file
    is enough — so defaulting it to "not configured" would be choosing to keep
    the "sync error: HTTP 404" the UI shows today. Every other capability
    genuinely needs wiring and correctly refuses until it has it.

    A host that DOES implement state keeps its own; this only fills the hole.
    """

    def __init__(self, inner: Engine, store: FileState) -> None:
        self._inner = inner
        self._store = store

    def __getattr__(self, name: str) -> Any:      # delegate everything else
        return getattr(self._inner, name)

    def stream_chat(self, messages: list[dict], **opts: Any) -> Iterator[str]:
        return self._inner.stream_chat(messages, **opts)

    def models(self) -> list[dict]:
        return self._inner.models()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)

    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        return self._inner.web_search(query, max_results)

    def load_state(self) -> Optional[dict]:
        try:
            return self._inner.load_state()
        except NotConfigured:
            return self._store.load()

    def save_state(self, blob: dict) -> None:
        try:
            self._inner.save_state(blob)
        except NotConfigured:
            self._store.save(blob)


def serve(ui_root: Path, engine: Engine, port: int = 11434,
          host: str = "127.0.0.1", verbose: bool = False,
          state: Optional[Path] = None,
          models: Optional[ModelManager] = None) -> ThreadingHTTPServer:
    """Start the server. Binds loopback by default — local-first is the point."""
    store = FileState(state or (ui_root / ".gobbonet-state.json"))
    bound = _StateBackedEngine(engine, store)
    handler: Callable[..., BaseHTTPRequestHandler] = type(
        "BoundHandler", (Handler,), {
            "engine": bound,
            "ui_root": ui_root,
            # Default it ON: the picker is part of the app, and a user who
            # installed the pack to run GobboNet off Windows wants the whole
            # app, not the chat box with a dead dropdown above it.
            "models_mgr": models if models is not None else ModelManager(),
        })
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except (PermissionError, OSError) as e:
        # On Windows this is NOT the rare case it looks like: Hyper-V, WSL and
        # Docker reserve whole BLOCKS of ports, so a bind inside one fails with
        # `WinError 10013 ... forbidden by its access permissions` while netstat
        # shows nothing listening. The port looks free, the error says
        # "permission", and neither points at the cause. Measured live on 11497.
        #
        # Unhandled, a user gets a stack trace about access permissions and
        # concludes the tool is broken. Fall back to an OS-assigned port; the
        # CALLER must then read httpd.server_address for the real one rather
        # than echoing the port it asked for.
        #   netsh interface ipv4 show excludedportrange protocol=tcp
        print(f"could not bind port {port} ({type(e).__name__}: {e}); letting the OS choose")
        httpd = ThreadingHTTPServer((host, 0), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    return httpd


class AgenticEngine(AgenticEngineMixin, Engine):
    """The full lift: adk's ReAct loop, tools and memory behind GobboNet's chat.

    This is what makes the pack worth installing rather than a config change.
    GobboNet keeps its UI, its character cards and its local-first promise; the
    chat box is answered by an agent with the tool registry, memory and packs
    behind it. The UI needs no changes because the seam is the
    OpenAI-compatible API it already speaks.

    Search and state come from the plain engine below, unchanged.
    """

    agent_identity = "gobbonet"

    def __init__(self, backend: Optional[str] = None, exclude_port: Optional[int] = None):
        # The agent brings its own LLM routing, but embeddings still come from
        # whatever local server is running — GobboNet's retriever needs them and
        # degrades SILENTLY without them.
        self._local = LocalEngine(backend=backend, exclude_port=exclude_port)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._local.embed(texts)

    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        return _DefaultEngine().web_search(query, max_results)


class LocalEngine(Engine):
    """Keyless search AND chat, against whatever local model server you have.

    A chat client with no model behind it is a strange thing to hand someone,
    and that is what this pack was before: it served the UI, answered search,
    and refused to chat. Everything needed already shipped in adk — it just was
    not wired up.

    The backend is DISCOVERED, not configured: llama.cpp, ollama, vLLM and LM
    Studio all speak the OpenAI-compatible API that GobboNet itself speaks, so
    finding one is a port scan rather than an integration. Pass `backend=` to
    pin a specific URL instead.

    Discovery is lazy on purpose. Probing at construction time would make
    `serve()` fail on a machine where the model server starts a few seconds
    later — a common ordering, and an unnecessary reason to refuse.
    """

    def __init__(self, backend: Optional[str] = None, exclude_port: Optional[int] = None):
        self._pinned = backend
        self._exclude_port = exclude_port
        self._found = None

    def _backend(self):
        from adk.packs.gobbonet import backend as be

        if self._pinned:
            # A pinned URL is still probed — pointing at a dead port and
            # discovering it one turn later, mid-conversation, is worse than
            # refusing now with the reason.
            # THE WHOLE URL, not a port scraped off the end of it. This used to
            # be `int(self._pinned.rsplit(':', 1)[-1])`, which discarded the
            # host -- so every pinned backend was probed on localhost, and a
            # URL without a port raised ValueError on `//host/v1`.
            found = be._probe(self._pinned, "pinned")
            if not found:
                raise NotConfigured(f"nothing usable at {self._pinned}\n\n{be.setup_hint()}")
            return found
        if self._found is None:
            self._found = be.discover(exclude_port=self._exclude_port)
        if self._found is None:
            raise NotConfigured(be.setup_hint())
        return self._found

    def stream_chat(self, messages: list[dict], **opts: Any) -> Iterator[str]:
        from adk.packs.gobbonet import backend as be

        yield from be.stream_completion(self._backend(), messages, **opts)

    def embed(self, texts: list[str]) -> list[list[float]]:
        from adk.packs.gobbonet import backend as be

        return be.embed(self._backend(), texts)

    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        return _DefaultEngine().web_search(query, max_results)


class _DefaultEngine(Engine):
    """What you get by running this module directly: adk's own keyless search.

    This is the answer to GobboNet's account problem, and it is why the module
    is runnable rather than being only a library. GobboNet's web search asks the
    user for an Ollama account and an API key, which contradicts the product's
    own "No account, no sign-up, no email".

    adk ships `web_search` — DuckDuckGo through a maintained client, no key, no
    account, running on the user's machine. Pointing GobboNet's SEARCH_URL at
    this server therefore removes the account requirement without adding a
    dependency on anybody's hosted service, ours included.

    Only web_search is wired here. Chat, models and embeddings still refuse
    honestly, because a host that wants those should supply its own Engine
    rather than inherit a guess.
    """

    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        try:
            import asyncio

            from adk.builtin_tools import web_search as _adk_search
        except Exception as exc:  # pragma: no cover - adk not importable
            raise NotConfigured(f"web_search (adk unavailable: {exc})") from exc

        raw = asyncio.run(_adk_search(query, limit=max_results))
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        out = []
        for r in payload.get("results", [])[:max_results]:
            # adk returns `snippet`; GobboNet's UI reads `content`. Mapping it
            # here is the whole reason a result arrives with text rather than a
            # bare link — the same field-name trap this file already documents.
            out.append({
                "title": r.get("title", ""),
                "url": r.get("url", "") or r.get("href", ""),
                "content": r.get("snippet", "") or r.get("body", ""),
            })
        return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ui", required=True, type=Path,
                    help="directory holding GobboNet's chat.html, css/ and js/")
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--state", type=Path, default=None,
                    help="conversation store (default: <ui>/.gobbonet-state.json)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not (args.ui / "chat.html").is_file():
        print(f"no chat.html under {args.ui} — point --ui at a GobboNet checkout")
        return 2

    httpd = serve(args.ui, _DefaultEngine(), port=args.port, host=args.host,
                  verbose=args.verbose, state=args.state)
    print(f"GobboNet UI on http://{args.host}:{args.port}/chat.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

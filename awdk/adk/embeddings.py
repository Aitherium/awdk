"""Canonical, self-deploying embeddings provider for awdk.

ONE embedding provider for the whole SDK — every agent, fleet, and company brain
that needs vectors resolves through here, so vectors are PORTABLE across scopes
(platform / tenant / sovereign): same model + same dimension everywhere.

Canonical model id: ``nomic-embed-text`` — the vLLM ``--served-model-name`` for
nomic-ai/nomic-embed-text-v1.5 (768-dim). The fleet serves it on vLLM over the
internal-TLS mesh (``aither-vllm-embeddings:8209``, https) and Ollama serves the
same 768-d model under the same id. NB: requesting the HF root
``nomic-embed-text-v1.5`` returns 404 — always use the served id. The ONLY
dimension-incompatible rung is the
degraded CPU fallback (all-MiniLM-L6-v2, 384-dim) — every returned batch is tagged
with its dimension so callers NEVER silently mix 768-d and 384-d vectors in one
index (which poisons cosine similarity).

Resolution chain (first that works wins; resolved once per process, then cached):
  1. Explicit endpoint      — ``AITHER_EMBEDDINGS_URL`` (operator override; the
                              fleet/gateway/BYO OpenAI-compatible ``/v1/embeddings``).
  2. Local vLLM             — ``localhost:8209`` then ``:8120``, HTTPS then HTTP
                              (internal-TLS mesh serves :8209 over https).
  3. Local Ollama           — ``nomic-embed-text`` on ``:11434`` (768-d, dev box).
  4. Gateway (metered)      — ``AITHER_GATEWAY_EMBEDDINGS_URL`` (managed fallback).
  5. Auto-deploy local vLLM — if a GPU + Docker are present and nothing above is
                              reachable, spin up the embeddings container on :8209
                              (opt in with ``AITHER_EMBED_AUTODEPLOY=1``; disabled by default
                              so resolution happens instantly offline).
  6. CPU sentence-transformers (384-d, DEGRADED, dim-tagged) — offline last resort.
  7. Feature-hash (384-d, DEGRADED) — always works, zero deps; keeps offline
                              semantic search alive when even ST is unavailable.

Usage:
    from adk.embeddings import get_default_embedder, embed_texts

    # As an injected embedder for GraphMemory (async (text) -> vector):
    gm = GraphMemory(agent_name="atlas", embedder=get_default_embedder())

    vectors, dim = await embed_texts(["hello", "world"])

Everything is best-effort and MUST NOT raise into the caller — a failed rung falls
through to the next; the final rung (feature-hash) cannot fail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from typing import Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger("adk.embeddings")

__all__ = [
    "CANONICAL_MODEL",
    "CANONICAL_DIM",
    "AdkEmbeddings",
    "get_provider",
    "get_default_embedder",
    "embed_texts",
    "embed_one",
    "reset_provider",
]

# ─────────────────────────────────────────────────────────────────────────────
# Canonical pins
# ─────────────────────────────────────────────────────────────────────────────

# The SERVED model id (vLLM --served-model-name), NOT the HF root. The fleet's
# vLLM embeddings worker serves nomic-ai/nomic-embed-text-v1.5 UNDER the id
# "nomic-embed-text" — requesting "nomic-embed-text-v1.5" returns 404. Verified
# live against aither-vllm-embeddings :8209.
CANONICAL_MODEL = os.getenv("AITHER_EMBED_MODEL", "nomic-embed-text")
CANONICAL_DIM = 768  # nomic-ai/nomic-embed-text-v1.5
_DEGRADED_DIM = 384  # all-MiniLM-L6-v2 / feature-hash — DIM-INCOMPATIBLE, tagged

# Ports the fleet/setup_cli use for a local vLLM embeddings worker.
_VLLM_EMBED_PORT = 8209  # dedicated embeddings worker (setup_cli 'full' tier)
_VLLM_GENERIC_PORT = 8120
_OLLAMA_PORT = 11434
_OLLAMA_EMBED_MODEL = "nomic-embed-text"  # Ollama's name for the same 768-d model

_HTTP_TIMEOUT = 30.0
# 15s, not 3s: backend RESOLUTION IS CACHED FOR THE PROCESS LIFETIME, so one
# probe that times out pins that service to a degraded 384-d fallback until it
# restarts — and a consumer holding a 768-d index then stores NOTHING at all.
# Measured 2026-07-31: a COLD vLLM embeddings backend answers its first call in
# ~2.3s and every later call in 94-287ms, so a 3.0s probe was a coin-flip
# against a cold backend, and losing it was silent and permanent. A slow probe
# costs one-time startup latency; losing it costs the service its embeddings.
_PROBE_TIMEOUT = float(os.getenv("AITHER_EMBED_PROBE_TIMEOUT", "15"))
_BATCH = 64

# Auto-deploy tuning
_AUTODEPLOY_IMAGE = os.getenv("AITHER_EMBED_VLLM_IMAGE", "vllm/vllm-openai:latest")
_AUTODEPLOY_CONTAINER = "aither-adk-embeddings"
_AUTODEPLOY_HEALTH_BUDGET = float(os.getenv("AITHER_EMBED_AUTODEPLOY_TIMEOUT", "180"))


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "on", "yes", "auto")


def _local_vllm_host(port: int) -> str:
    """The host to dial for a local vLLM embeddings worker.

    Inside a container, ``localhost`` is the container itself — the fleet
    name is the only reachable spelling. Outside, the port is published on
    the host loopback. Same marker as lib.core.AitherPorts._container_marker.
    """
    if os.path.exists("/run/.containerenv"):
        return f"aither-vllm-embeddings:{port}"
    return f"localhost:{port}"


# ─────────────────────────────────────────────────────────────────────────────
# Feature-hash fallback (offline, zero-dep, DEGRADED 384-d)
# ─────────────────────────────────────────────────────────────────────────────

def _feature_hash(text: str, dim: int = _DEGRADED_DIM) -> List[float]:
    """Deterministic-ish feature-hash embedding. Offline, no model.

    Mirrors GraphMemory's historical fallback so switching to this provider does
    not change offline behaviour. Note: uses the builtin ``hash`` (salted per
    process) — vectors are only comparable WITHIN a process, which is exactly the
    scope of a single search, so this is acceptable for the degraded rung.
    """
    vector = [0.0] * dim
    for word in text.lower().split():
        vector[hash(word) % dim] += 1.0
        if len(word) > 3:
            vector[hash(word[:3]) % dim] += 0.5
    mag = sum(x * x for x in vector) ** 0.5
    if mag > 0:
        vector = [x / mag for x in vector]
    return vector


# ─────────────────────────────────────────────────────────────────────────────
# Provider
# ─────────────────────────────────────────────────────────────────────────────

class AdkEmbeddings:
    """Lazily-resolved, dimension-tagged embeddings provider (async).

    Resolution is single-flight and cached for the life of the process (so the
    active dimension is stable). Call :func:`reset_provider` to force a re-probe
    (e.g. after a backend comes online).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._resolved = False
        self._backend: str = "unresolved"   # vllm | ollama | gateway | cpu | hash
        self._url: str = ""
        self._model: str = CANONICAL_MODEL
        self._dim: int = 0
        self._degraded: bool = False
        self._st_model = None               # cached sentence-transformers model
        self._autodeploy_attempted = False

    # ── public state ────────────────────────────────────────────────────
    @property
    def dim(self) -> int:
        return self._dim

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def backend(self) -> str:
        return self._backend

    def describe(self) -> dict:
        return {
            "backend": self._backend,
            "url": self._url,
            "model": self._model,
            "dim": self._dim,
            "degraded": self._degraded,
            "canonical_dim": CANONICAL_DIM,
        }

    # ── resolution ──────────────────────────────────────────────────────
    async def _resolve(self) -> None:
        if self._resolved:
            return
        async with self._lock:
            if self._resolved:
                return
            backend = await self._pick_backend()
            self._backend = backend
            self._resolved = True
            if self._degraded:
                logger.warning(
                    "adk.embeddings: DEGRADED backend=%s dim=%d (NOT the canonical "
                    "%d-d %s — vectors are not portable to a real embeddings backend)",
                    backend, self._dim, CANONICAL_DIM, CANONICAL_MODEL,
                )
            else:
                logger.info(
                    "adk.embeddings: backend=%s url=%s dim=%d model=%s",
                    backend, self._url, self._dim, self._model,
                )

    async def _pick_backend(self) -> str:
        # 1. explicit endpoint
        explicit = os.getenv("AITHER_EMBEDDINGS_URL", "").strip()
        if explicit and await self._try_openai_endpoint(explicit):
            return "vllm"

        # 2. local vLLM — dedicated embeddings port, then generic. Try HTTPS first
        # (the internal TLS mesh serves :8209 over https w/ the internal CA), then
        # plain HTTP (a bare dev vLLM / our own auto-deployed container).
        for port in (_VLLM_EMBED_PORT, _VLLM_GENERIC_PORT):
            if await self._try_local_vllm(port):
                return "vllm"

        # 3. local Ollama (768-d, dev box)
        if await self._try_ollama(f"http://localhost:{_OLLAMA_PORT}"):
            return "ollama"

        # 4. gateway (metered managed fallback)
        gw = os.getenv("AITHER_GATEWAY_EMBEDDINGS_URL", "").strip()
        if gw and await self._try_openai_endpoint(gw):
            return "gateway"

        # 5. auto-deploy a local vLLM embeddings container (GPU + Docker)
        # NOTE: opt-IN (disabled by default) so resolution is instant offline
        if _flag("AITHER_EMBED_AUTODEPLOY", False) and await self._maybe_autodeploy():
            if await self._try_openai_endpoint(f"http://localhost:{_VLLM_EMBED_PORT}"):
                return "vllm"

        # 6. CPU sentence-transformers (DEGRADED 384-d)
        if _flag("AITHER_EMBED_ALLOW_CPU", True) and self._probe_sentence_transformers():
            self._dim = _DEGRADED_DIM
            self._degraded = True
            self._model = os.getenv("AITHER_EMBED_ST_MODEL", "all-MiniLM-L6-v2")
            return "cpu"

        # 7. feature-hash (DEGRADED 384-d) — never fails
        self._dim = _DEGRADED_DIM
        self._degraded = True
        self._model = "feature-hash"
        return "hash"

    # ── backend probes ──────────────────────────────────────────────────
    def _norm_base(self, base: str) -> str:
        base = base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base

    async def _try_local_vllm(self, port: int) -> bool:
        """Probe a local vLLM embeddings worker on ``port``, HTTPS then HTTP.

        In-container the host is the FLEET NAME, never localhost — measured
        2026-08-27/28: localhost inside a container is the container itself, so
        adk's baked probe made genesis/worker/nexus/strata fall to the 384-d
        degraded tail while the embeddings lane answered 200. Re-landed
        2026-08-28 (the first landing was lost in the 08-27 worktree revert).
        """
        for scheme in ("https", "http"):
            if await self._try_openai_endpoint(f"{scheme}://{_local_vllm_host(port)}"):
                return True
        return False

    async def _try_openai_endpoint(self, base: str) -> bool:
        """Probe an OpenAI-compatible ``/v1/embeddings`` endpoint with a real call
        so we learn the actual dimension. Returns True and pins state on success."""
        try:
            import httpx
        except ImportError:
            return False
        base = self._norm_base(base)
        verify = _tls_verify()
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, verify=verify) as c:
                r = await c.post(
                    f"{base}/v1/embeddings",
                    json={"input": ["ping"], "model": CANONICAL_MODEL},
                )
                if r.status_code != 200:
                    return False
                data = r.json().get("data", [])
                if not data:
                    return False
                vec = data[0].get("embedding") or []
                if not vec:
                    return False
                self._url = base
                self._model = CANONICAL_MODEL
                self._dim = len(vec)
                self._degraded = self._dim != CANONICAL_DIM
                return True
        except Exception as exc:  # noqa: BLE001 — probe: any failure = not this rung
            logger.debug("openai embed probe %s failed: %s", base, exc)
            return False

    async def _try_ollama(self, base: str) -> bool:
        try:
            import httpx
        except ImportError:
            return False
        base = base.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as c:
                r = await c.post(
                    f"{base}/api/embeddings",
                    json={"model": _OLLAMA_EMBED_MODEL, "prompt": "ping"},
                )
                if r.status_code != 200:
                    return False
                vec = r.json().get("embedding") or []
                if not vec:
                    return False
                self._url = base
                self._model = _OLLAMA_EMBED_MODEL
                self._dim = len(vec)
                self._degraded = self._dim != CANONICAL_DIM
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("ollama embed probe %s failed: %s", base, exc)
            return False

    def _probe_sentence_transformers(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    # ── auto-deploy ─────────────────────────────────────────────────────
    async def _maybe_autodeploy(self) -> bool:
        """Best-effort: if a GPU + Docker are present and :8209 is free, start the
        canonical vLLM embeddings container and wait (bounded) for it to come up.

        Never raises. Single attempt per process. The container pull can be large,
        so we bound the wait — if it isn't healthy in budget, this call returns
        False and the caller uses a fallback for now; a later call re-probes :8209.
        """
        if self._autodeploy_attempted:
            return False
        self._autodeploy_attempted = True

        # _has_gpu() shells out (nvidia-smi) — run off the event loop.
        if not await asyncio.to_thread(_has_gpu):
            logger.debug("autodeploy skipped: no GPU detected")
            return False
        docker = shutil.which("docker")
        if not docker:
            logger.debug("autodeploy skipped: docker not found")
            return False
        # If something already owns the port, don't fight it — probe handles it.
        if _port_in_use(_VLLM_EMBED_PORT):
            return False

        logger.warning(
            "adk.embeddings: no embeddings backend reachable — auto-deploying vLLM "
            "embeddings (%s) on :%d (autodeploy is opt-IN via AITHER_EMBED_AUTODEPLOY=1)",
            _AUTODEPLOY_IMAGE, _VLLM_EMBED_PORT,
        )
        try:
            # Remove any stale container of the same name first (ignore errors).
            # Both docker calls are blocking — run them off the event loop so the
            # resolution lock (held here) doesn't stall concurrent embed callers.
            await asyncio.to_thread(
                subprocess.run,
                [docker, "rm", "-f", _AUTODEPLOY_CONTAINER],
                capture_output=True, timeout=20, check=False,
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    docker, "run", "-d", "--name", _AUTODEPLOY_CONTAINER,
                    "--gpus", "all", "--restart", "unless-stopped",
                    "-p", f"{_VLLM_EMBED_PORT}:{_VLLM_EMBED_PORT}",
                    _AUTODEPLOY_IMAGE,
                    "--model", "nomic-ai/nomic-embed-text-v1.5",
                    "--served-model-name", CANONICAL_MODEL,
                    "--port", str(_VLLM_EMBED_PORT),
                    "--dtype", "float16", "--max-num-seqs", "64",
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if proc.returncode != 0:
                logger.warning("autodeploy docker run failed: %s", proc.stderr[:300])
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("autodeploy failed: %s", exc)
            return False

        # Bounded wait for readiness (async sleep so we don't block the loop).
        deadline = time.monotonic() + _AUTODEPLOY_HEALTH_BUDGET
        while time.monotonic() < deadline:
            if _port_in_use(_VLLM_EMBED_PORT) and await self._openai_ready(
                f"http://localhost:{_VLLM_EMBED_PORT}"
            ):
                logger.info("adk.embeddings: auto-deployed vLLM embeddings is ready")
                return True
            await asyncio.sleep(5)
        logger.warning(
            "adk.embeddings: auto-deployed container not ready within %.0fs — "
            "falling back for now (will use it on a later call)",
            _AUTODEPLOY_HEALTH_BUDGET,
        )
        return False

    async def _openai_ready(self, base: str) -> bool:
        try:
            import httpx
        except ImportError:
            return False
        base = self._norm_base(base)
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as c:
                r = await c.get(f"{base}/v1/models")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    # ── embedding ───────────────────────────────────────────────────────
    async def embed_texts(self, texts: List[str]) -> Tuple[List[List[float]], int]:
        """Embed ``texts``. Returns ``(vectors, dim)``. Never raises.

        The returned ``dim`` is authoritative — callers MUST key their index on it
        and must not compare vectors of different dims.
        """
        if not texts:
            await self._resolve()
            return [], self._dim
        await self._resolve()
        try:
            if self._backend in ("vllm", "gateway"):
                vecs = await self._embed_openai(texts)
            elif self._backend == "ollama":
                vecs = await self._embed_ollama(texts)
            elif self._backend == "cpu":
                vecs = self._embed_st(texts)
            else:
                vecs = [_feature_hash(t) for t in texts]
            if vecs is not None:
                return vecs, self._dim
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the caller
            logger.warning("adk.embeddings backend=%s failed: %s", self._backend, exc)

        # Backend failed at call time (was reachable at probe). Re-resolve next
        # call; for THIS call degrade to the dim-consistent fallback we can honour.
        self._resolved = False
        if self._dim == CANONICAL_DIM:
            # Can't produce 768-d offline — skip rather than mix dims. Empty vectors
            # let dimension-aware consumers (GraphMemory) drop the embedding safely.
            return [[] for _ in texts], self._dim
        return [_feature_hash(t) for t in texts], self._dim

    async def embed_one(self, text: str) -> List[float]:
        """Embed a single string → vector (the injected-embedder shape GraphMemory
        expects). Returns ``[]`` if the backend can only produce a different dim
        (so GraphMemory skips the embedding instead of mixing dims)."""
        vecs, _dim = await self.embed_texts([text])
        return vecs[0] if vecs else []

    async def _embed_openai(self, texts: List[str]) -> Optional[List[List[float]]]:
        import httpx
        verify = _tls_verify()
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            batch = texts[i:i + _BATCH]
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=verify) as c:
                r = await c.post(
                    f"{self._url}/v1/embeddings",
                    json={"input": batch, "model": self._model},
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
                out.extend(d["embedding"] for d in r.json().get("data", []))
        return out

    async def _embed_ollama(self, texts: List[str]) -> Optional[List[List[float]]]:
        import httpx
        out: List[List[float]] = []
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
            for t in texts:
                r = await c.post(
                    f"{self._url}/api/embeddings",
                    json={"model": self._model, "prompt": t},
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                out.append(r.json().get("embedding") or [])
        return out

    def _embed_st(self, texts: List[str]) -> List[List[float]]:
        if self._st_model is None:
            from sentence_transformers import SentenceTransformer
            self._st_model = SentenceTransformer(self._model)
        return self._st_model.encode(texts, show_progress_bar=False).tolist()


# ─────────────────────────────────────────────────────────────────────────────
# Environment helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tls_verify():
    """Trust the internal CA for mesh TLS (never disable verification). Falls
    back to default verification when the awkit TLS helper isn't importable."""
    try:
        from portal_kit_backend.tls import internal_verify
        return internal_verify()
    except Exception:  # noqa: BLE001
        return True


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _has_gpu() -> bool:
    """Detect an NVIDIA/AMD/Apple GPU. Reuses adk.setup_cli.detect_gpu when
    available; falls back to an ``nvidia-smi`` probe."""
    try:
        from adk.setup_cli import detect_gpu
        gpu = detect_gpu()
        return getattr(gpu, "vendor", "none") not in ("none", "", None)
    except Exception:  # noqa: BLE001
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(
                    ["nvidia-smi", "-L"], capture_output=True, timeout=5, check=False
                )
                return r.returncode == 0 and bool(r.stdout.strip())
            except Exception:  # noqa: BLE001
                return False
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton + convenience API
# ─────────────────────────────────────────────────────────────────────────────

_provider: Optional[AdkEmbeddings] = None


def get_provider() -> AdkEmbeddings:
    """Get (or create) the process-wide embeddings provider singleton."""
    global _provider
    if _provider is None:
        _provider = AdkEmbeddings()
    return _provider


def get_default_embedder() -> Callable[[str], Awaitable[List[float]]]:
    """Return the canonical async ``(text) -> vector`` embedder — the shape
    :class:`adk.graph_memory.GraphMemory` accepts as its ``embedder=`` argument."""
    return get_provider().embed_one


async def embed_texts(texts: List[str]) -> Tuple[List[List[float]], int]:
    """Embed a batch via the canonical provider. Returns ``(vectors, dim)``."""
    return await get_provider().embed_texts(texts)


async def embed_one(text: str) -> List[float]:
    """Embed a single string via the canonical provider."""
    return await get_provider().embed_one(text)


def reset_provider() -> None:
    """Drop the cached provider so the next call re-probes the resolution chain."""
    global _provider
    _provider = None

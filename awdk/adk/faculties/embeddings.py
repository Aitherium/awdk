"""
EmbeddingProvider — Pluggable Embedding Backends for ADK
=========================================================

Provides embedding vectors with automatic backend detection:
  1. sentence-transformers (local GPU/CPU, best quality)
  2. Ollama /api/embeddings (local, no extra deps)
  3. Elysium gateway.aitherium.com/v1/embeddings (cloud, needs API key)
  4. Feature hashing (zero deps, worst quality, always works)

Usage:
    from adk.faculties.embeddings import get_embedding_provider

    provider = get_embedding_provider()
    vec = await provider.embed("Hello world")
    vecs = await provider.embed_batch(["a", "b", "c"])
"""

import asyncio
import hashlib
import logging
import math
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.faculties.embeddings")

_EMBEDDING_DIM = 768  # Default dimension (nomic-embed-text)
_FEATURE_HASH_DIM = 768  # Feature hash output dimension
_MODEL_NAME = os.getenv("AITHER_EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1.5")
_OLLAMA_URL_RAW = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_OLLAMA_URL = _OLLAMA_URL_RAW.replace("0.0.0.0", "localhost") if "0.0.0.0" in _OLLAMA_URL_RAW else _OLLAMA_URL_RAW
if not _OLLAMA_URL.startswith("http"):
    _OLLAMA_URL = "http://" + _OLLAMA_URL
_ELYSIUM_URL = os.getenv("AITHER_ELYSIUM_URL", "https://gateway.aitherium.com")


class EmbeddingProvider:
    """Pluggable embedding backend with automatic fallback chain."""

    def __init__(self, model_name: str = _MODEL_NAME):
        self.model_name = model_name
        self._backend: Optional[str] = None
        self._st_model = None  # sentence-transformers model
        self._lock: Optional[asyncio.Lock] = None
        self._ollama_available: Optional[bool] = None
        self._elysium_available: Optional[bool] = None
        self.stats: Dict[str, Any] = {
            "embed_calls": 0,
            "total_texts": 0,
            "backend": "pending",
        }

    @property
    def _async_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _try_load_sentence_transformers(self) -> bool:
        """Try to load sentence-transformers model. Returns True on success."""
        if self._st_model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            device = "cpu"
            try:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
            except ImportError:
                pass
            logger.info("Loading embedding model: %s on %s", self.model_name, device)
            self._st_model = SentenceTransformer(
                self.model_name, trust_remote_code=True, device=device,
            )
            if device == "cuda":
                self._st_model.half()
            self._backend = f"sentence-transformers ({device})"
            self.stats["backend"] = self._backend
            return True
        except ImportError:
            return False
        except Exception as e:
            logger.warning("Failed to load sentence-transformers: %s", e)
            return False

    async def _embed_st(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed via sentence-transformers (in thread to avoid blocking)."""
        def _encode():
            vecs = self._st_model.encode(
                texts, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
            return [v.tolist() for v in vecs]
        return await asyncio.to_thread(_encode)

    async def _embed_ollama(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed via Ollama /api/embeddings."""
        if self._ollama_available is False:
            return [None] * len(texts)
        try:
            import httpx
            results = []
            async with httpx.AsyncClient(timeout=15.0) as client:
                for text in texts:
                    resp = await client.post(
                        f"{_OLLAMA_URL}/api/embeddings",
                        json={"model": "nomic-embed-text", "prompt": text},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        results.append(data.get("embedding"))
                        if self._ollama_available is None:
                            self._ollama_available = True
                            self._backend = "ollama"
                            self.stats["backend"] = "ollama"
                            logger.info("Using Ollama embedding backend")
                    else:
                        results.append(None)
                        if self._ollama_available is None:
                            self._ollama_available = False
            return results
        except Exception as e:
            if self._ollama_available is None:
                logger.debug("Ollama embeddings not available: %s", e)
                self._ollama_available = False
            return [None] * len(texts)

    async def _embed_elysium(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed via Elysium cloud /v1/embeddings (OpenAI-compatible)."""
        if self._elysium_available is False:
            return [None] * len(texts)
        api_key = os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            self._elysium_available = False
            return [None] * len(texts)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_ELYSIUM_URL}/v1/embeddings",
                    json={"input": texts, "model": "nomic-embed-text"},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vecs = [None] * len(texts)
                    for item in data.get("data", []):
                        idx = item.get("index", 0)
                        if idx < len(vecs):
                            vecs[idx] = item.get("embedding")
                    if self._elysium_available is None:
                        self._elysium_available = True
                        self._backend = "elysium"
                        self.stats["backend"] = "elysium"
                        logger.info("Using Elysium cloud embedding backend")
                    return vecs
                else:
                    if self._elysium_available is None:
                        self._elysium_available = False
                    return [None] * len(texts)
        except Exception as e:
            if self._elysium_available is None:
                logger.debug("Elysium embeddings not available: %s", e)
                self._elysium_available = False
            return [None] * len(texts)

    @staticmethod
    def _feature_hash(text: str, dim: int = _FEATURE_HASH_DIM) -> List[float]:
        """Deterministic feature hashing -- zero deps, always works."""
        vec = [0.0] * dim
        words = text.lower().split()
        for word in words:
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % dim
            sign = 1.0 if (h >> 128) % 2 == 0 else -1.0
            vec[idx] += sign
        # Normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    async def embed(self, text: str) -> Optional[List[float]]:
        """Embed a single text. Tries backends in priority order."""
        if not text or not text.strip():
            return None
        self.stats["embed_calls"] += 1
        self.stats["total_texts"] += 1
        results = await self.embed_batch([text])
        return results[0] if results else None

    async def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed multiple texts. Uses the best available backend."""
        if not texts:
            return []
        self.stats["total_texts"] += len(texts)

        # 1. sentence-transformers
        async with self._async_lock:
            if self._st_model is not None or self._try_load_sentence_transformers():
                return await self._embed_st(texts)

        # 2. Ollama
        results = await self._embed_ollama(texts)
        if any(r is not None for r in results):
            return results

        # 3. Elysium
        results = await self._embed_elysium(texts)
        if any(r is not None for r in results):
            return results

        # 4. Feature hashing (always works)
        if self._backend != "feature_hash":
            self._backend = "feature_hash"
            self.stats["backend"] = "feature_hash"
            logger.info(
                "Using feature hashing fallback"
                " (install sentence-transformers for better quality)"
            )
        return [self._feature_hash(t) if t and t.strip() else None for t in texts]

    def get_status(self) -> Dict[str, Any]:
        """Return current backend status and usage statistics."""
        return {
            "backend": self._backend or "not_initialized",
            "model": self.model_name,
            **self.stats,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_provider: Optional[EmbeddingProvider] = None


def get_embedding_provider(**kwargs: Any) -> EmbeddingProvider:
    """Return the module-level singleton EmbeddingProvider, creating it if needed."""
    global _provider
    if _provider is None:
        _provider = EmbeddingProvider(**kwargs)
    return _provider


def reset_embedding_provider() -> None:
    """Reset singleton (for testing)."""
    global _provider
    _provider = None

"""CodeGraph — re-exported from the `awgraph` package, with ADK's own host hooks.

This module used to be a 2,800-line FORK of the same engine. It has been replaced
by a re-export, so the ADK now runs the code that is published, tested and
benchmarked as `awgraph` rather than a copy that drifted from it.

WHY THE FORK WAS A PROBLEM
--------------------------
There were three independent CodeGraph implementations: the platform's
(`lib/faculties/CodeGraph.py`), the published package (`awgraph`), and this fork.
The ADK declared no dependency on `awgraph` at all, so agents indexed code with an
implementation ~2,000 lines behind the engine the platform serves — and nothing
compared them, so a fix in one reached the others only if someone remembered.

That is not a hypothetical. The same shape in `awgit` left **14 of 17 modules
drifted**, including a `capture.py` that had lost its "unparseable is not
deletion" guard and recorded every function in a conflicted file as deleted —
confidently wrong data in a log meant to be authoritative.

Every public name this module used to export exists in `awgraph`, and `awgraph`'s
`CodeGraph` is a strict superset (30 public methods against this fork's 16, with
none missing), so the swap is a drop-in for every caller.

WHY THIS RAISES INSTEAD OF DEGRADING
------------------------------------
`awgraph` is a declared dependency of this package, so it is present in any
correct install. If it is somehow absent, this raises with the fix in the message
rather than falling back to a stub. A silent stub is how a feature becomes inert
while every probe stays green — the failure this codebase spends the most effort
avoiding.

THE HOOK ASYMMETRY THIS FIXES
------------------------------
The re-export above was a drop-in for CALLERS, but it never called
`awgraph.plugins.configure(...)` — so it silently kept awgraph's stdlib defaults:
plain `httpx.AsyncClient` (no internal-CA/mTLS trust) and no embedding backend
(keyword-only search, -0.121 recall@25 on this codebase per awgraph's own
benchmark). `lib/faculties/CodeGraph.py` on the platform side DOES configure real
hooks, so ADK-driven coding agents — the ones actually doing edit work — were
running the degraded half while genesis-side platform callers got the enriched
one. This module now wires ADK's own equivalents, reusing existing ADK code
rather than inventing new trust/embedding logic:

  * async_client  -> `adk.client._client`'s mTLS device-cert + internal-CA
                      resolution (`adk.sync.device_identity`, `adk._tls.tls_verify`),
                      wrapped as an httpx.AsyncClient subclass so it matches the
                      bare `AsyncClient(timeout=...)` calling convention awgraph
                      uses internally.
  * embedding_engine -> `adk.embeddings.get_provider()`, adapted to awgraph's
                      `engine.embed_batch(texts, model=...) -> list[vector|None]`
                      shape (ADK's provider returns `(vectors, dim)` and never
                      raises; a length mismatch or total failure degrades to
                      `[None] * len(texts)` so awgraph's own "all-None means
                      failed" logging fires instead of silently mixing dims).
  * logger_factory -> stdlib `logging.getLogger`, kept explicit (not left as
                      awgraph's default) so the logger name matches ADK's own
                      `adk.<module>` convention.

ADK must never import monorepo `lib.*` (ADK002/AWP002 boundary — see
`check_adk_publishable.py`), so none of this reaches into `lib/faculties/
_host_hooks.py` on the platform side; it is self-contained in `adk.*`.
"""

from __future__ import annotations

import logging as _logging
from typing import Any, List, Optional


def _adk_logger_factory(name: str):
    return _logging.getLogger(name)


def _adk_async_client_class():
    """An httpx.AsyncClient subclass carrying ADK's mTLS device cert + internal
    CA trust, reusing exactly what `adk.client._client.AitherClient._get_client`
    already does — not new trust logic. Falls back to plain httpx (system CA)
    if no device cert is enrolled, same as the existing client does.
    """
    import os

    import httpx

    class _ADKAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            if "cert" not in kwargs:
                cert_path = os.environ.get("AITHER_NODE_CERT")
                if not cert_path:
                    try:
                        from adk.sync import device_identity

                        cert_tuple = device_identity.load_device_cert()
                        if cert_tuple:
                            cert_path = cert_tuple
                    except Exception:
                        cert_path = None
                if cert_path:
                    kwargs["cert"] = cert_path
                    if "verify" not in kwargs:
                        try:
                            from adk._tls import tls_verify

                            kwargs["verify"] = tls_verify()
                        except Exception as exc:
                            _logging.getLogger("adk.faculties.code_graph").debug(
                                "adk._tls.tls_verify unavailable (%s) — falling "
                                "back to system CA verification", exc
                            )
            super().__init__(**kwargs)

    return _ADKAsyncClient


class _ADKEmbeddingEngine:
    """Adapts `adk.embeddings.get_provider()` to awgraph's
    `engine.embed_batch(texts, model=...) -> list[vector | None]` contract.
    """

    async def embed_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[Optional[list]]:
        from adk.embeddings import get_provider

        vectors, _dim = await get_provider().embed_texts(texts)
        if len(vectors) != len(texts):
            # Total or partial failure ADK's provider signals by short count —
            # awgraph's own contract wants a same-length list with None entries
            # so its "all-None means the backend failed" logging fires, rather
            # than a length mismatch it was never written to expect.
            return [None] * len(texts)
        return vectors


def _adk_embedding_engine_factory():
    return _ADKEmbeddingEngine()


import awgraph.plugins as _plugins  # noqa: E402

_plugins.configure(
    logger_factory=_adk_logger_factory,
    async_client=_adk_async_client_class(),
    embedding_engine=_adk_embedding_engine_factory,
)

try:
    from awgraph import CodeChunk, CodeGraph
    from awgraph.graph import (
        ELASTIC_AGENT,
        ELASTIC_REASON,
        ELASTIC_REFLEX,
        CallExtractor,
        ChunkType,
        FileGraph,
        discover_python_files,
        estimate_complexity,
        extract_calls,
        get_codegraph,
        get_signature,
        logger,
        main,
        parse_file_sync,
        reindex_files,
    )
except ImportError as exc:  # pragma: no cover - exercised by a broken install
    raise ImportError(
        "adk.faculties.code_graph now re-exports the `awgraph` package, which is "
        "a declared dependency of awdk but could not be imported: "
        f"{exc}. Install it with `pip install awgraph` (or reinstall awdk). "
        "This deliberately raises rather than falling back to a stub — a stubbed "
        "code graph returns empty results, which reads as 'the repository has "
        "nothing matching' instead of 'the indexer is missing'."
    ) from exc

__all__ = [
    "CallExtractor",
    "ChunkType",
    "CodeChunk",
    "CodeGraph",
    "ELASTIC_AGENT",
    "ELASTIC_REASON",
    "ELASTIC_REFLEX",
    "FileGraph",
    "discover_python_files",
    "estimate_complexity",
    "extract_calls",
    "get_codegraph",
    "get_signature",
    "logger",
    "main",
    "parse_file_sync",
    "reindex_files",
]

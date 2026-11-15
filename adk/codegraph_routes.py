"""Serve this daemon's warm code graph to other processes on the host.

The daemon already holds ONE awgraph index for its own recall and code tools
(``builtin_tools._local_code_graph``). Without these routes every ``awgraph query`` a
terminal or agent session ran loaded its own full copy of the same index -- gigabytes
and tens of seconds per call. ``awgraph`` asks here first (``awgraph.shared``) and
loads locally only when this daemon is absent or serves a different root.

    GET  /v1/codegraph/info   {"root": <abs root or null>, "warm": bool, "reason": str}
    POST /v1/codegraph/query  {"q", "k", "root"} -> {"hits": [...], "source"}
         409 root mismatch · 503 {"error": "loading"} while the graph warms ·
         503 {"error": "deferred", "deferred": true, "reason"} when the graph worker
         put its load off (host memory pressure, another full load of the root) ·
         503 {"error": "no_graph"} when this daemon has no graph · 502 query failed

A deferral is NOT a failure for the caller to route around: the worker deferred
because loading the index now would push the host over its memory limit, and a
client that answered by loading the same index itself would do exactly that.

Read-only. Behind the daemon's normal auth middleware like every other route.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger("adk.codegraph_routes")

#: Largest ``k`` served; the tool path caps at the same value.
MAX_K = 50
#: How the graph worker words a deferred load (its ready line and the reason it keeps).
DEFERRED_MARK = "deferred:"


def _deferral(*texts: str) -> str:
    """The deferral reason in any of ``texts`` (from ``deferred:`` on), or ""."""
    for text in texts:
        i = (text or "").find(DEFERRED_MARK)
        if i >= 0:
            return text[i:][:300]
    return ""


def _kick_reopen(idx: Any) -> None:
    """Let the index retry its open in the background, never inside this request.

    A graph that failed or deferred retries only from ``idx._open()`` (after its
    retry window). Answering "deferred" without calling it froze the graph at
    "deferred" until something else asked (measured 2026-11-08: the index run that
    caused the deferral had exited, and every query still answered deferred).
    Awaiting the open here instead would hold the request for a full hydrate,
    and a client that times out on that would load the index itself.
    """
    opener = getattr(idx, "_open", None)
    if not callable(opener):
        return
    task = getattr(idx, "_route_reopen", None)
    if task is not None and not task.done():
        return

    def _done(t: "asyncio.Future[Any]") -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.warning("code graph reopen failed: %s", t.exception())

    task = asyncio.ensure_future(opener())
    task.add_done_callback(_done)
    idx._route_reopen = task


def _deferred_response(reason: str) -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "error": "deferred", "deferred": True, "reason": reason})


def _graph() -> Any:
    from adk.builtin_tools import _local_code_graph

    return _local_code_graph()


def _same_root(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@router.get("/v1/codegraph/info")
async def codegraph_info() -> dict:
    idx = _graph()
    if idx is None:
        return {"root": None, "warm": False, "reason": "no code graph bound"}
    return {"root": str(getattr(idx, "root", "") or "") or None,
            "warm": bool(getattr(idx, "is_warm", False)),
            "reason": str(getattr(idx, "reason", "") or "")}


@router.post("/v1/codegraph/query")
async def codegraph_query(body: dict) -> Any:
    text = str(body.get("q") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "empty_query"})
    try:
        k = max(1, min(int(body.get("k") or 10), MAX_K))
    except (TypeError, ValueError):
        k = 10
    idx = _graph()
    if idx is None:
        return JSONResponse(status_code=503, content={"error": "no_graph"})
    served = str(getattr(idx, "root", "") or "")
    asked = str(body.get("root") or "")
    if asked and not _same_root(asked, served):
        return JSONResponse(status_code=409,
                            content={"error": "root_mismatch", "root": served})
    # A worker that deferred answers "deferred" at once, and its retry is kicked in
    # the background so the NEXT query can find it warm.
    held = _deferral(str(getattr(idx, "reason", "") or ""))
    if held and getattr(idx, "_ok", None) is False:
        _kick_reopen(idx)
        return _deferred_response(held)
    from adk.builtin_tools import _local_graph_hits

    out = await _local_graph_hits(text, k)
    if out is None:
        return JSONResponse(status_code=503, content={"error": "no_graph"})
    err = str(out.get("error") or "")
    if err:
        held = _deferral(err, str(getattr(idx, "reason", "") or ""))
        if held:
            return _deferred_response(held)
        loading = "still loading" in err
        return JSONResponse(status_code=503 if loading else 502,
                            content={"error": "loading" if loading else "query_failed",
                                     "detail": err[:300]})
    return {"hits": out.get("results") or [], "source": "adk-daemon", "root": served}

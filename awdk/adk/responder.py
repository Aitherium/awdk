"""Canonical instant-response loop for AitherADK.

The "answer now, enrich in the background, auto-continue" capability the whole
platform shares. ONE implementation — used by AitherAgent.stream_respond,
Genesis chat, and awkit, instead of each reinventing it.

  Phase A — INITIAL  : immediately stream a fast first-pass answer on minimal
                       context, so there is NO dead air before the first token.
  Phase B — ENRICH   : concurrently run the full grounded pipeline / agentic
                       loop (tools, RAG, memory). Its events stream live so the
                       user can watch the machinery work.
  Phase C — CONTINUE : when the grounded answer is MATERIALLY different from the
                       first pass (tools were used, artifacts produced, content
                       diverged), stream it as a refinement segment that
                       auto-continues the turn. If the first pass already nailed
                       it, the refine is skipped — the fast path stays fast.

Event protocol (emitted via ``on_event``)::

    {"event":"answer_segment","segment_index":N,"kind":"initial"|"refinement"}
    {"event":"token","t":"…","segment_index":N}
    {"event":"segment_end","segment_index":N}
    …live enrich events (thinking / tool / tool_result / …) are forwarded…
    {"event":"complete","segments":1|2,"duration_ms":N}

Backend-agnostic: the caller supplies the first-pass streamer and the enrich
coroutine, so this module has no AitherOS/Genesis import dependency. After the
first segment is emitted we are COMMITTED to this mode (the client is already
rendering) — failures degrade to the grounded answer, never raise.
"""

from __future__ import annotations

import asyncio
import difflib
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from adk.grounding import ground_system_prompt


# ── Refinement decision (shared so Genesis/awkit/ADK agree) ─────────────

def text_similarity(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def materially_different(
    initial: str, grounded: str, *, used_tools: bool, has_artifacts: bool,
) -> bool:
    """Whether the grounded answer adds enough to warrant a second segment."""
    g = (grounded or "").strip()
    if not g:
        return False
    if not (initial or "").strip():
        return True                       # first pass empty → grounded is primary
    if used_tools or has_artifacts:
        return True                       # real grounding happened → show it
    return text_similarity(initial, grounded) < 0.82  # only if genuinely diverged


def additive_delta(initial: str, grounded: str) -> str:
    """If the grounded answer opens by echoing the first pass, keep only the new
    tail so the refinement doesn't restate what the user already read."""
    i, g = (initial or "").strip(), (grounded or "").strip()
    if g.startswith(i) and len(g) > len(i):
        return g[len(i):].lstrip()
    return g


# ── Orchestration ────────────────────────────────────────────────────────────

# () -> async iterator yielding answer-text chunks (the fast first pass)
FirstPassStream = Callable[[], AsyncIterator[str]]
# (on_event) -> grounded result dict; runs the full pipeline concurrently
EnrichFn = Callable[[Callable[[dict], Awaitable[None]]], Awaitable[dict]]
OnEvent = Callable[[dict], Awaitable[None]]


async def _emit(on_event: OnEvent, evt: dict) -> None:
    try:
        r = on_event(evt)
        if asyncio.iscoroutine(r):
            await r
    except Exception:  # noqa: BLE001
        pass


async def respond(
    *,
    message: str,
    on_event: OnEvent,
    first_pass_stream: FirstPassStream,
    enrich: EnrichFn,
    direct_answer: Optional[Callable[[], Awaitable[str]]] = None,
    enrich_timeout_s: float = 90.0,
    agent: str = "aither",
) -> dict:
    """Run the instant-response → background-enrich → auto-continue loop.

    ``first_pass_stream()`` → async iterator of answer chunks (fast, minimal
    context). ``enrich(on_enrich_event)`` → runs the grounded pipeline
    concurrently and returns ``{"answer", "used_tools", "artifacts"}`` (extra
    keys ignored). ``direct_answer()`` is an optional non-streaming fallback used
    when the first-pass stream yields nothing (stream flakiness) so the fast path
    is guaranteed. Returns ``{"answer", "segments", "duration_ms"}``.
    """
    import time as _time
    _t0 = _time.perf_counter()

    # ── Phase B kicks off FIRST so enrichment overlaps the first pass ──
    enrich_events: "asyncio.Queue[dict]" = asyncio.Queue()

    async def _on_enrich_event(evt: dict) -> None:
        # The grounded pipeline's own answer/token/complete are swallowed here —
        # the responder owns the answer segments + the terminal complete.
        et = (evt or {}).get("type") or (evt or {}).get("event")
        if et in ("answer", "final_answer", "complete", "token", "done"):
            return
        try:
            enrich_events.put_nowait(evt)
        except Exception:  # noqa: BLE001
            pass

    enrich_task = asyncio.ensure_future(enrich(_on_enrich_event))

    # ── Phase A — INITIAL: stream the fast first pass ──
    seg = 0
    initial_parts: list[str] = []
    await _emit(on_event, {"event": "answer_segment", "segment_index": seg,
                           "kind": "initial", "agent": agent, "reason": "fast first pass"})
    _n = 0
    try:
        async for chunk in first_pass_stream():
            if not chunk:
                continue
            initial_parts.append(chunk)
            _n += 1
            await _emit(on_event, {"event": "token", "t": chunk, "n": _n,
                                   "agent": agent, "segment_index": seg})
    except Exception:  # noqa: BLE001 — committed to eager mode; degrade, don't raise
        pass

    initial_answer = "".join(initial_parts).strip()

    # Reliability: streamed first pass intermittently returns zero tokens. Use the
    # non-streaming fallback so the fast path is GUARANTEED (never a 40-60s hang).
    if not initial_answer and direct_answer is not None:
        try:
            _fb = (await direct_answer()) or ""
        except Exception:  # noqa: BLE001
            _fb = ""
        if _fb:
            for _i in range(0, len(_fb), 8):
                _n += 1
                await _emit(on_event, {"event": "token", "t": _fb[_i:_i + 8], "n": _n,
                                       "agent": agent, "segment_index": seg})
            initial_answer = _fb

    await _emit(on_event, {"event": "segment_end", "segment_index": seg, "agent": agent})

    # ── Phase B drain — forward live enrich events until the pipeline finishes ──
    grounded: dict = {}
    async def _drain_until_done() -> None:
        while not enrich_task.done() or not enrich_events.empty():
            try:
                evt = await asyncio.wait_for(enrich_events.get(), timeout=0.15)
                await _emit(on_event, evt)
            except asyncio.TimeoutError:
                continue
    try:
        await asyncio.wait_for(asyncio.gather(_drain_until_done(), enrich_task),
                               timeout=enrich_timeout_s)
        res = enrich_task.result() if enrich_task.done() else None
        if isinstance(res, dict):
            grounded = res
    except asyncio.TimeoutError:
        if not enrich_task.done():
            enrich_task.cancel()
    except Exception:  # noqa: BLE001
        pass

    # ── Phase C — CONTINUE: refine only if the grounding materially added value ──
    segments = 1
    grounded_answer = str(grounded.get("answer", "") or "")
    final_answer = initial_answer
    if materially_different(
        initial_answer, grounded_answer,
        used_tools=bool(grounded.get("used_tools")),
        has_artifacts=bool(grounded.get("artifacts")),
    ):
        seg = 1
        segments = 2
        delta = additive_delta(initial_answer, grounded_answer) if initial_answer else grounded_answer
        await _emit(on_event, {"event": "answer_segment", "segment_index": seg,
                               "kind": "refinement", "agent": agent})
        for _i in range(0, len(delta), 8):
            _n += 1
            await _emit(on_event, {"event": "token", "t": delta[_i:_i + 8], "n": _n,
                                   "agent": agent, "segment_index": seg})
        await _emit(on_event, {"event": "segment_end", "segment_index": seg, "agent": agent})
        final_answer = grounded_answer or initial_answer

    duration_ms = int((_time.perf_counter() - _t0) * 1000)
    await _emit(on_event, {"event": "complete", "segments": segments,
                           "duration_ms": duration_ms, "agent": agent})
    return {"answer": final_answer, "segments": segments, "duration_ms": duration_ms}


async def _strip_think(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield chunks with any ``<think>…</think>`` span removed, tolerant of tags
    split across chunk boundaries. ``/no_think`` usually suppresses thinking, but
    this guarantees the fast first pass never leaks reasoning into the UI."""
    in_think = False
    buf = ""
    async for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        out = ""
        while buf:
            if in_think:
                end = buf.find("</think>")
                if end == -1:
                    buf = buf[-8:] if len(buf) > 8 else buf  # keep a possible split tag
                    break
                buf = buf[end + len("</think>"):]
                in_think = False
            else:
                start = buf.find("<think>")
                if start == -1:
                    # emit all but a tail that might be a partial opening tag
                    keep = 7 if len(buf) >= 7 else len(buf)
                    out += buf[:len(buf) - keep]
                    buf = buf[len(buf) - keep:]
                    break
                out += buf[:start]
                buf = buf[start + len("<think>"):]
                in_think = True
        if out:
            yield out
    if buf and not in_think:
        yield buf


# (messages, max_tokens) -> full text ; and -> async iterator of chunks
OneShotFn = Callable[[list, int], Awaitable[str]]
StreamFn = Callable[[list, int], AsyncIterator[str]]
ClassifyFn = Callable[[], Awaitable[Any]]
RunGroundedFn = Callable[[Any, OnEvent], Awaitable[dict]]


async def stream_chat(
    *,
    message: str,
    on_event: OnEvent,
    classify: ClassifyFn,
    oneshot: OneShotFn,
    stream: StreamFn,
    run_grounded: RunGroundedFn,
    persona: str = "",
    name: str = "Aither",
    history: Optional[list] = None,
    agent: str = "aither",
    enrich_timeout_s: float = 120.0,
) -> dict:
    """Canonical honest instant-response chat — the ONE glue both bots share.

    Classify FIRST, then shape the response so it is always HONEST:

    - grounded loop will run (``agentic`` or ``requires_grounding``) → the REAL
      answer comes from the background, so the first pass is a DETERMINISTIC honest
      ack ("Let me check {label}…"). Instant, never fabricates, no ``<think>`` delay.
    - otherwise (pure chitchat / general knowledge) → a direct no-think LLM answer,
      told to never fabricate specifics it doesn't have.

    The grounded answer auto-continues as a refinement segment (Phase C). The
    grounded lane keeps its FULL reasoning and gets a generous token budget upstream
    — there is no synthesis fallback (the cure for truncation is budget, not a
    band-aid).

    Bot-supplied primitives keep this backend-agnostic:
      ``classify()`` → IntentDecision-like (``agentic``/``effort``/
      ``requires_grounding``/``grounding_label``); ``oneshot(msgs, max)`` /
      ``stream(msgs, max)`` → the bot's no-think LLM (caller appends ``/no_think``
      or the helper does); ``run_grounded(decision, on_ev)`` → the bot's grounded
      ReAct loop returning ``{"answer","used_tools","artifacts"}``.
    """
    decision = await classify()
    grounding = bool(getattr(decision, "requires_grounding", False))
    agentic = bool(getattr(decision, "agentic", False))
    depth = (getattr(decision, "reasoning_depth", "") or "").strip().lower()
    label = (getattr(decision, "grounding_label", "") or "").strip()

    def _nothink(msgs: list) -> list:
        out = [dict(m) for m in msgs]
        for m in reversed(out):
            if m.get("role") == "user":
                m["content"] = (str(m.get("content", "")) + " /no_think").strip()
                break
        return out

    async def _first_pass() -> AsyncIterator[str]:
        # Honest ack whenever the grounded loop will produce the real answer.
        if grounding or agentic:
            yield (f"Let me check {label} for you…" if label
                   else "On it — let me work through that for you…")
            return
        base = (persona or f"You are {name}, a helpful assistant.").rstrip()
        sysmsg = ground_system_prompt(base) + (
            "\n\nAnswer the user directly and concisely RIGHT NOW from what you "
            "genuinely know. Never fabricate specifics (names, dates, numbers, data) "
            "you don't actually have."
        )
        msgs = [{"role": "system", "content": sysmsg}]
        for h in (history or [])[-6:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
                msgs.append({"role": h["role"], "content": str(h["content"])[:2000]})
        msgs.append({"role": "user", "content": message})
        async for chunk in _strip_think(stream(_nothink(msgs), 512)):
            if chunk:
                yield chunk

    async def _direct() -> str:
        base = (persona or f"You are {name}.").rstrip()
        msgs = [{"role": "system", "content": ground_system_prompt(base)},
                {"role": "user", "content": message}]
        from re import sub as _sub
        txt = await oneshot(_nothink(msgs), 512)
        return _sub(r"<think>.*?</think>", "", txt or "", flags=16).strip()  # 16 = DOTALL

    async def _enrich(on_ev: OnEvent) -> dict:
        # Run the grounded/agentic lane on SEMANTIC NEED, never on an effort
        # threshold. It runs whenever the turn needs tools (agentic), external
        # data (requires_grounding), or real reasoning (reasoning_depth beyond
        # skip/gate). Only genuinely trivial chat — no grounding, no tools, no
        # reasoning — lets the (already system-state-grounded) first pass stand.
        # Effort scales the loop (step budget), it does NOT gate whether it runs.
        _trivial = (not grounding and not agentic and depth in ("", "skip", "gate"))
        if _trivial:
            return {"answer": "", "used_tools": False, "artifacts": []}
        res = await run_grounded(decision, on_ev) or {}
        return {"answer": str(res.get("answer", "") or "").strip(),
                "used_tools": bool(res.get("used_tools")),
                "artifacts": res.get("artifacts") or []}

    return await respond(
        message=message, on_event=on_event,
        first_pass_stream=_first_pass, enrich=_enrich, direct_answer=_direct,
        enrich_timeout_s=enrich_timeout_s, agent=agent,
    )


__all__ = [
    "respond", "stream_chat", "materially_different", "additive_delta",
    "text_similarity", "FirstPassStream", "EnrichFn", "OnEvent",
    "OneShotFn", "StreamFn", "ClassifyFn", "RunGroundedFn",
]

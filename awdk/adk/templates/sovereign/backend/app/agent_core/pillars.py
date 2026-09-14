"""MiniSixPillarsKernel — compact 6-pillar cognition loop for standalone agents.

Mirrors AitherOS lib/core/SixPillarsKernel.py at small scale. Provides a
single `tick()` entrypoint that chat handlers can call as a backbone for
intent capture, context assembly, reasoning execution, and learning
write-back. Heavy reasoning is delegated to the host app's `llm.chat()`
callable, which is injected at construction time.

Pillars:
    P1 Intent       — classify, extract entities, set effort.
    P2 Context      — assemble (memory recall + RAG + status) in <8s.
    P3 Reasoning    — invoke LLM with assembled context.
    P4 Orchestration— optional tool dispatch hook (callable injected).
    P5 Creation     — finalize the response payload.
    P6 Learning     — store memory + emit telemetry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .client import UnifiedMemoryClient
from .tiers import MemoryTier

log = logging.getLogger("agent_core.pillars")


# Type aliases for the host-app callbacks the kernel uses.
LLMCallable = Callable[..., Awaitable[str]]
RAGCallable = Callable[[str], Awaitable[list[dict[str, Any]]]]
ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class PillarResult:
    response: str
    intent: dict[str, Any] = field(default_factory=dict)
    context_used: list[dict[str, Any]] = field(default_factory=list)
    memory_recalled: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    memory_id: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "response": self.response,
            "intent": self.intent,
            "context_used": self.context_used,
            "memory_recalled": self.memory_recalled,
            "sources": self.sources,
            "memory_id": self.memory_id,
            "timings_ms": self.timings_ms,
            "errors": self.errors,
        }


class MiniSixPillarsKernel:
    """Small-app cognition kernel. One instance per FastAPI process."""

    def __init__(
        self,
        memory: UnifiedMemoryClient,
        llm_chat: LLMCallable,
        rag_search: RAGCallable | None = None,
        tool_dispatch: ToolDispatcher | None = None,
        *,
        context_budget_seconds: float = 8.0,
        reasoning_budget_seconds: float = 300.0,
        memory_recall_limit: int = 6,
        rag_top_k: int = 6,
    ):
        self.memory = memory
        self.llm_chat = llm_chat
        self.rag_search = rag_search
        self.tool_dispatch = tool_dispatch
        self.context_budget = context_budget_seconds
        self.reasoning_budget = reasoning_budget_seconds
        self.memory_recall_limit = memory_recall_limit
        self.rag_top_k = rag_top_k

    # ── P1: Intent ─────────────────────────────────────────────────────────

    def classify_intent(self, message: str) -> dict[str, Any]:
        msg = (message or "").strip()
        lower = msg.lower()
        intent_type = "chat"
        if any(lower.startswith(p) for p in ("/", "!")):
            intent_type = "command"
        elif any(k in lower for k in ("delete ", "remove ", "drop ")):
            intent_type = "action"
        elif lower.endswith("?") or lower.startswith(("how ", "what ", "why ", "when ", "where ", "who ")):
            intent_type = "question"
        effort = 1
        if len(msg) > 240:
            effort = 4
        elif len(msg) > 80:
            effort = 2
        if intent_type == "action":
            effort = max(effort, 3)
        return {
            "type": intent_type,
            "length": len(msg),
            "effort": effort,
        }

    # ── P2: Context (<8s budget) ───────────────────────────────────────────

    async def assemble_context(
        self,
        tenant_id: str,
        message: str,
        *,
        conversation_id: str | None = None,
        extra_status: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (memory_recalled, rag_chunks, sources) within the budget."""
        recalled: list[dict[str, Any]] = []
        rag_chunks: list[dict[str, Any]] = []

        async def _recall():
            results = await self.memory.recall(
                tenant_id, message, limit=self.memory_recall_limit,
            )
            return [r.as_dict() for r in results]

        async def _rag():
            if not self.rag_search:
                return []
            res = self.rag_search(message)
            if asyncio.iscoroutine(res):
                res = await res
            return res or []

        try:
            recalled, rag_chunks = await asyncio.wait_for(
                asyncio.gather(_recall(), _rag(), return_exceptions=False),
                timeout=self.context_budget,
            )
        except asyncio.TimeoutError:
            log.debug("context budget exceeded — using partial results")
        except Exception as e:
            log.debug("context assembly failed: %s", e)

        sources: list[dict[str, Any]] = []
        for c in rag_chunks or []:
            if not isinstance(c, dict):
                continue
            meta = c.get("metadata") or {}
            sources.append({
                "type": meta.get("type") or "document",
                "filename": c.get("filename") or meta.get("source") or "uploaded",
                "score": round(float(c.get("score") or 0.0), 3),
            })
        return recalled or [], rag_chunks or [], sources

    # ── P3 + P5: Reasoning + Creation ──────────────────────────────────────

    async def execute_reasoning(
        self,
        *,
        message: str,
        history: list[dict[str, str]],
        rag_chunks: list[dict[str, Any]],
        memory_recalled: list[dict[str, Any]],
        system_extra: str = "",
    ) -> str:
        memory_text = ""
        if memory_recalled:
            lines = [
                f"- ({m.get('tier', 'working')}/{m.get('source', 'local')}) "
                f"{(m.get('content') or '')[:280]}"
                for m in memory_recalled[: self.memory_recall_limit]
            ]
            memory_text = "\n\n[RELEVANT MEMORY]\n" + "\n".join(lines)
        prompt_extra = system_extra + memory_text
        try:
            result = await asyncio.wait_for(
                self.llm_chat(
                    user_message=message,
                    context_chunks=rag_chunks,
                    conversation_history=(history or [])[-10:],
                    system_extra=prompt_extra,
                ),
                timeout=self.reasoning_budget,
            )
        except TypeError:
            # Fallback for hosts whose llm.chat has a slightly different signature.
            result = await self.llm_chat(message)
        except asyncio.TimeoutError:
            return "Reasoning timed out. Please try again."
        return str(result or "").strip()

    # ── P6: Learning ───────────────────────────────────────────────────────

    async def learn(
        self,
        *,
        tenant_id: str,
        conversation_id: str | None,
        message: str,
        response_text: str,
        document_citations: list[str] | None = None,
    ) -> str | None:
        if len(response_text) < 80:
            return None
        confidence = 0.55 if len(response_text) >= 200 else 0.45
        summary = (
            f"Q: {message[:160].strip()} -> A: {response_text[:280].strip()}"
        )
        try:
            return await self.memory.store_memory(
                tenant_id, summary,
                tier=MemoryTier.WORKING,
                conversation_id=conversation_id,
                source="conversation",
                confidence=confidence,
                credibility=0.65,
                metadata={"length": len(response_text)},
                document_citations=document_citations,
            )
        except Exception as e:
            log.debug("learn() failed: %s", e)
            return None

    # ── Public entrypoint ──────────────────────────────────────────────────

    async def tick(
        self,
        *,
        tenant_id: str,
        message: str,
        conversation_id: str | None = None,
        history: list[dict[str, str]] | None = None,
        system_extra: str = "",
        document_citations: list[str] | None = None,
    ) -> PillarResult:
        timings: dict[str, float] = {}
        out = PillarResult(response="")

        t0 = time.perf_counter()
        out.intent = self.classify_intent(message)
        timings["p1_intent"] = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        recalled, rag_chunks, sources = await self.assemble_context(
            tenant_id, message, conversation_id=conversation_id,
        )
        out.memory_recalled = recalled
        out.context_used = rag_chunks
        out.sources = sources
        timings["p2_context"] = (time.perf_counter() - t1) * 1000

        t2 = time.perf_counter()
        try:
            out.response = await self.execute_reasoning(
                message=message,
                history=history or [],
                rag_chunks=rag_chunks,
                memory_recalled=recalled,
                system_extra=system_extra,
            )
        except Exception as e:
            out.errors.append(f"reasoning: {e}")
            out.response = (
                "I hit an error generating the response. Please try again."
            )
        timings["p3_reasoning"] = (time.perf_counter() - t2) * 1000

        # P4 orchestration is opt-in — bound only when tools needed.
        timings["p4_orchestration"] = 0.0

        # P5 creation = trivially packaging out (already done above).
        timings["p5_creation"] = 0.0

        t3 = time.perf_counter()
        try:
            out.memory_id = await self.learn(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message=message,
                response_text=out.response,
                document_citations=document_citations,
            )
        except Exception as e:
            out.errors.append(f"learn: {e}")
        timings["p6_learning"] = (time.perf_counter() - t3) * 1000

        out.timings_ms = timings
        return out

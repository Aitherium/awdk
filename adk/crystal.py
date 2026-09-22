"""Context is memory, not a window (orchestration spec section 8, owner 2026-09-22).

The loop's context is EMBEDDED, RECALLED and CRYSTALLIZED through the memory planes that
already exist, instead of being summarized into a throwaway message and forgotten:

  crystallize   when Layer 2 compaction folds old history into a summary, every fact in
                that summary is written to awm at the task's scope
                (``aitherium:<agent>:<task>``). What was learned about a file, a failing
                test or a rejected approach survives the window AND the session.
  recall        each turn begins with what memory already knows about this task: the
                nearest facts (microembeddings when an embedder is bound, keyword overlap
                otherwise) plus the code-graph symbols that match the request (awgraph),
                injected as one system block under a hard character cap.

Every plane degrades HONESTLY: a missing awm, awgraph or embedder is logged once and
counted in ``telemetry["degraded"]``, never silently skipped, so a run that recalled
nothing says why. Nothing here raises into the agent loop -- a memory fault must not kill
a turn -- but every fault is visible in the telemetry the harness records.

Measured motivation (2026-09-22, L3d reflex-4129): 28 ``file_read`` of 47 steps re-read
files the loop had already seen and summarized away; the compaction summary itself was
discarded at the end of the run. Both are what this module keeps.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("adk.crystal")

#: Facts shorter than this are noise ("Done.", "See above"); longer than the ceiling are
#: a pasted tool result, not a fact.
FACT_MIN_CHARS = 24
FACT_MAX_CHARS = 400
#: How many facts one compaction may write. A 600-char-per-message summary of 30
#: messages yields ~20 lines; 40 leaves headroom without letting a runaway summary
#: flood the scope.
MAX_FACTS_PER_COMPACTION = 40
#: Recall reads at most this many rows from the scope before ranking.
SCAN_LIMIT = 200
#: Vectors are stored rounded: 768 floats at 4 decimals is ~5 KB per fact.
VEC_DECIMALS = 4

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{3,}")


class FactStore(Protocol):
    """The minimal store the crystal needs; ``AwmFactStore`` is the shipped one."""

    def put(self, key: str, value: str, meta: dict) -> None: ...

    def scan(self, limit: int) -> list[tuple[str, str, dict]]: ...


class GraphIndex(Protocol):
    async def query(self, text: str, k: int) -> list[dict]: ...


Embedder = Callable[[list[str]], Awaitable[list[Optional[list[float]]]]]


# ── awm adapter ──────────────────────────────────────────────────────────────

class AwmFactStore:
    """awm-backed store at exactly one scope. Reads see the scope and its ancestors."""

    def __init__(self, scope: str, db: Path | str | None = None):
        # Guarded: awm is a sibling brick, not an awdk dependency. Off-fleet the wheel
        # must import; the plane is then reported missing (ADK002 allows guarded imports).
        from awm.scope import Scope  # type: ignore[import-not-found]
        from awm.store import MemoryStore  # type: ignore[import-not-found]

        self._scope = Scope.parse(scope)
        path = Path(db) if db else Path.home() / ".aither" / "awm" / "memory.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._store = MemoryStore(path)
        self.path = path

    def put(self, key: str, value: str, meta: dict) -> None:
        self._store.remember(self._scope, key, value, kind="crystal", meta=meta)

    def scan(self, limit: int) -> list[tuple[str, str, dict]]:
        rows = self._store.recall(self._scope, limit=limit, kind="crystal")
        return [(m.key, m.value, dict(m.meta or {})) for m in rows]

    def close(self) -> None:
        try:
            self._store.close()
        except Exception:  # noqa: BLE001
            pass


class AwgraphIndex:
    """awgraph over a repository root, opened lazily once (hydrating a large index
    costs tens of seconds; measured 34.7 s on 387k chunks)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._graph: Any = None
        self._ok: bool | None = None

    async def _open(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            from awgraph.cli import _open_graph  # type: ignore[import-not-found]

            self._graph, self._ok = await _open_graph(self.root, build=False)
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            logger.warning("[CRYSTAL] awgraph unavailable for %s: %s", self.root, exc)
            self._ok = False
        return bool(self._ok)

    async def query(self, text: str, k: int) -> list[dict]:
        if not await self._open():
            raise RuntimeError(f"no awgraph index for {self.root}")
        chunks = await self._graph.hybrid_query(text, max_results=k)
        out: list[dict] = []
        for c in chunks:
            path = getattr(c, "source_path", "") or ""
            try:
                path = os.path.relpath(path, self.root)
            except ValueError:
                pass
            out.append({
                "name": getattr(c, "name", ""),
                "path": path,
                "line": getattr(c, "start_line", 0),
                "signature": getattr(c, "signature", "") or "",
            })
        return out


def make_scheduler_embedder(url: str | None = None, model: str | None = None) -> Embedder:
    """An OpenAI-compatible ``/v1/embeddings`` embedder (MicroScheduler serves
    ``nomic-embed-text``; measured 2026-09-22 at https://127.0.0.1:8150/v1)."""
    base = (url or os.environ.get("ADK_EMBED_URL") or "https://127.0.0.1:8150/v1").rstrip("/")
    name = model or os.environ.get("ADK_EMBED_MODEL") or "nomic-embed-text"

    async def _embed(texts: list[str]) -> list[Optional[list[float]]]:
        import httpx

        try:
            from adk._tls import tls_verify

            verify: Any = tls_verify()
        except Exception:  # noqa: BLE001
            verify = True
        # One STRING per request: MicroScheduler's /v1/embeddings validates `input` as a
        # string and 422s the OpenAI list form (measured 2026-09-22). Sent concurrently.
        import asyncio

        async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
            async def _one(text: str) -> Optional[list[float]]:
                r = await client.post(f"{base}/embeddings", json={"model": name, "input": text})
                r.raise_for_status()
                data = r.json().get("data") or []
                vec = data[0].get("embedding") if data else None
                return [float(x) for x in vec] if vec else None

            return list(await asyncio.gather(*(_one(t) for t in texts)))

    return _embed


# ── pure helpers ─────────────────────────────────────────────────────────────

def split_facts(summary: str, *, limit: int = MAX_FACTS_PER_COMPACTION) -> list[str]:
    """Lines of a compaction summary that are worth keeping, deduplicated, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (summary or "").splitlines():
        line = _BULLET.sub("", raw).strip()
        if line.endswith(":") and len(line) < 40:
            continue  # a heading, not a fact
        if not (FACT_MIN_CHARS <= len(line) <= FACT_MAX_CHARS):
            continue
        norm = re.sub(r"\s+", " ", line.lower())
        if norm in seen:
            continue
        seen.add(norm)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def fact_key(fact: str) -> str:
    norm = re.sub(r"\s+", " ", fact.strip().lower())
    return "f:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def keyword_score(query: str, fact: str) -> float:
    q = {w.lower() for w in _WORD.findall(query)}
    f = {w.lower() for w in _WORD.findall(fact)}
    if not q or not f:
        return 0.0
    return len(q & f) / math.sqrt(len(q) * len(f))


# ── the crystal ──────────────────────────────────────────────────────────────

@dataclass
class Crystal:
    """One task's memory face: crystallize on compaction, recall at turn start."""

    scope: str
    store: Optional[FactStore] = None
    graph: Optional[GraphIndex] = None
    embed: Optional[Embedder] = None
    max_chars: int = 1500
    max_graph_hits: int = 6
    telemetry: dict = field(default_factory=lambda: {
        "writes": 0, "compactions": 0, "recalls": 0, "recalled_facts": 0,
        "graph_hits": 0, "degraded": [], "planes": {},
    })

    def __post_init__(self) -> None:
        self.telemetry["planes"] = {
            "awm": self.store is not None,
            "awgraph": self.graph is not None,
            "embed": self.embed is not None,
        }
        for plane, present in self.telemetry["planes"].items():
            if not present:
                self._degrade(f"{plane}:unbound")

    def _degrade(self, reason: str) -> None:
        if reason not in self.telemetry["degraded"]:
            self.telemetry["degraded"].append(reason)
            logger.warning("[CRYSTAL] plane degraded: %s", reason)

    async def _vectors(self, texts: list[str]) -> list[Optional[list[float]]]:
        if self.embed is None or not texts:
            return [None] * len(texts)
        try:
            vecs = await self.embed(texts)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"embed:{type(exc).__name__}")
            return [None] * len(texts)
        if len(vecs) != len(texts):
            self._degrade("embed:short-answer")
            return [None] * len(texts)
        return [([round(x, VEC_DECIMALS) for x in v] if v else None) for v in vecs]

    async def crystallize(self, summary: str) -> int:
        """Write every fact of a compaction summary to the scope. Returns the count."""
        self.telemetry["compactions"] += 1
        facts = split_facts(summary)
        if not facts:
            return 0
        if self.store is None:
            self._degrade("awm:unbound")
            return 0
        vecs = await self._vectors(facts)
        n = 0
        for fact, vec in zip(facts, vecs):
            meta: dict = {"src": "compaction", "ts": round(time.time(), 1)}
            if vec:
                meta["vec"] = vec
            try:
                self.store.put(fact_key(fact), fact, meta)
                n += 1
            except Exception as exc:  # noqa: BLE001
                self._degrade(f"awm:{type(exc).__name__}")
                break
        self.telemetry["writes"] += n
        logger.info("[CRYSTAL] crystallized %d/%d facts into %s", n, len(facts), self.scope)
        return n

    async def recall_facts(self, message: str, *, limit: int = 20) -> list[str]:
        if self.store is None:
            self._degrade("awm:unbound")
            return []
        try:
            rows = self.store.scan(SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awm:{type(exc).__name__}")
            return []
        if not rows:
            return []
        qvec = (await self._vectors([message]))[0] if self.embed is not None else None
        scored: list[tuple[float, float, str]] = []
        for _key, value, meta in rows:
            vec = meta.get("vec") if isinstance(meta, dict) else None
            if qvec is not None and vec:
                s = cosine(qvec, vec)
            else:
                s = keyword_score(message, value)
            ts = float(meta.get("ts", 0.0)) if isinstance(meta, dict) else 0.0
            scored.append((s, ts, value))
        # Best match first; a tie goes to the most recently established fact.
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [v for _s, _ts, v in scored[:limit]]

    async def recall_graph(self, message: str) -> list[dict]:
        if self.graph is None:
            return []
        try:
            return await self.graph.query(message, self.max_graph_hits)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awgraph:{type(exc).__name__}")
            return []

    async def recall_block(self, message: str) -> str:
        """The system block for a turn: facts + graph hits under ``max_chars``. "" = nothing."""
        self.telemetry["recalls"] += 1
        facts = await self.recall_facts(message)
        hits = await self.recall_graph(message)
        parts: list[str] = []
        used = 0
        kept_facts = 0
        if facts:
            head = ("[CRYSTAL] Facts this agent already established for this task, recalled "
                    "from memory. Build on them; re-verify only what a tool result contradicts.")
            parts.append(head)
            used += len(head)
            for f in facts:
                line = f"- {f}"
                if used + len(line) + 1 > self.max_chars:
                    break
                parts.append(line)
                used += len(line) + 1
                kept_facts += 1
        kept_hits = 0
        if hits:
            head = ("[CODE GRAPH] Symbols the code graph matched for this request. Open these "
                    "before any broad file read.")
            if used + len(head) + 1 <= self.max_chars:
                parts.append(head)
                used += len(head) + 1
                for h in hits:
                    sig = (h.get("signature") or h.get("name") or "").strip()
                    line = f"- {h.get('path', '')}:{h.get('line', 0)} {sig}"[:200]
                    if used + len(line) + 1 > self.max_chars:
                        break
                    parts.append(line)
                    used += len(line) + 1
                    kept_hits += 1
        self.telemetry["recalled_facts"] += kept_facts
        self.telemetry["graph_hits"] += kept_hits
        if kept_facts == 0 and kept_hits == 0:
            return ""
        return "\n".join(parts)


def build_crystal(scope: str, *, db: Path | str | None = None, graph_root: str | None = None,
                  embed_url: str | None = None, embed: bool = True) -> Crystal:
    """The shipped composition: awm store + awgraph index + scheduler embedder, each
    bound only if it can be, the rest reported as degraded on the returned crystal."""
    store: Optional[FactStore] = None
    graph: Optional[GraphIndex] = None
    degraded: list[str] = []
    try:
        store = AwmFactStore(scope, db)
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"awm:{type(exc).__name__}")
    if graph_root:
        graph = AwgraphIndex(graph_root)
    embedder = make_scheduler_embedder(embed_url) if embed else None
    c = Crystal(scope=scope, store=store, graph=graph, embed=embedder)
    for d in degraded:
        c._degrade(d)
    return c

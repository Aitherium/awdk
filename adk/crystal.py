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

Reconciled facts (awm >= 0.5): a fact whose subject is derivable -- it is written as
``<dotted.slot>: <value>`` or ``<dotted.slot> = <value>`` -- goes through awm's
``reconcile_and_remember`` so "ui.theme: light" SUPERSEDES "ui.theme: dark" (history
keeps the old value) instead of sitting beside it. ``SlotReconciler`` decides by default;
``LLMReconciler`` when a completion callable is bound (``Crystal.bind_completion``). Every
other fact keeps the legacy ``f:<hash>`` key, and legacy rows stay readable. The route is
chosen by FEATURE detection: a store without ``reconcile_and_remember``, or a file that
raises ``NeedsMigration`` (an older schema in compat mode), writes legacy rows and says
so in ``telemetry["degraded"]`` -- the file is never migrated from here.
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
#: ``<dotted.slot>: value`` / ``<dotted.slot> = value``. Two or more segments, letters
#: first, so a sentence ("Note: ...") or a path ("reflex/state.py: ...") never matches.
_SUBJECT = re.compile(
    r"^\s*`?([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+)`?\s*(?::|=)\s+(\S.*)$")
#: A last segment that names a file type means the "subject" is a filename.
_FILE_EXT = frozenset({"py", "pyi", "md", "js", "ts", "tsx", "jsx", "json", "yaml", "yml",
                       "toml", "txt", "cfg", "ini", "sh", "ps1", "psm1", "cjs", "mjs", "rs",
                       "go", "java", "c", "h", "cpp", "hpp", "cs", "html", "css", "sql",
                       "lock", "log", "csv", "xml", "env", "cmd", "bat"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{3,}")
#: What ``{agent}`` may NOT expand to in ``ADK_CRYSTAL_SCOPE``: a ``*`` (a wildcard
#: binds a shared ANCESTOR scope every sibling agent reads), a ``:`` (it shifts the
#: segments), a control character, or more than 128 characters. Anything else --
#: "Atlas Agent", "Emile" with an accent -- is one literal segment, bound exactly as
#: before the check existed, so an existing agent keeps its scope and its memory.
_AGENT_SEGMENT_BAD = re.compile(r"[*:\x00-\x1f\x7f]")
#: Refused ``{agent}`` names, for a host that cannot see a Crystal that was never built.
BIND_TELEMETRY: dict = {"refused": []}


class FactStore(Protocol):
    """The minimal store the crystal needs; ``AwmFactStore`` is the shipped one."""

    def put(self, key: str, value: str, meta: dict) -> None: ...

    def scan(self, limit: int) -> list[tuple[str, str, dict]]: ...


class GraphIndex(Protocol):
    async def query(self, text: str, k: int) -> list[dict]: ...


Embedder = Callable[[list[str]], Awaitable[list[Optional[list[float]]]]]


# ── awm adapter ──────────────────────────────────────────────────────────────

def derive_subject(fact: str) -> Optional[tuple[str, str]]:
    """``(subject, value)`` when the fact names its slot, else None.

    Conservative on purpose: a wrong subject makes a new fact SUPERSEDE an unrelated
    one, which is worse than keeping both under hash keys.
    """
    m = _SUBJECT.match(fact or "")
    if not m:
        return None
    subject, value = m.group(1), m.group(2).strip()
    segs = subject.split(".")
    if segs[-1].lower() in _FILE_EXT or any(seg.isdigit() for seg in segs):
        return None
    if not value:
        return None
    return subject, value


class AwmFactStore:
    """awm-backed store at exactly one scope. Reads see the scope and its ancestors."""

    def __init__(self, scope: str, db: Path | str | None = None,
                 complete: Optional[Callable[[str], str]] = None):
        # Guarded: awm is a sibling brick, not an awdk dependency. Off-fleet the wheel
        # must import; the plane is then reported missing (ADK002 allows guarded imports).
        from awm.scope import Scope  # type: ignore[import-not-found]
        from awm.store import MemoryStore  # type: ignore[import-not-found]

        self._scope = Scope.parse(scope)
        path = Path(db) if db else Path.home() / ".aither" / "awm" / "memory.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        # auto_migrate=False: an older file stays readable by its older readers.
        try:
            self._store = MemoryStore(path, auto_migrate=False)
        except TypeError:  # an awm that predates the flag
            self._store = MemoryStore(path)
        self.path = path
        self._complete = complete
        #: Feature detection, not a version compare: the method must exist AND work on
        #: this file (a compat-mode file raises NeedsMigration and flips this off).
        self.can_reconcile = callable(getattr(self._store, "reconcile_and_remember", None))
        #: True while can_reconcile is off ONLY because the file is an older schema: a
        #: migration run by another process while this handle is open turns it back on.
        self._needs_migration = False

    def _recheck_reconcile(self) -> None:
        """Re-enable reconcile once the file this handle holds has been migrated."""
        if self.can_reconcile or not self._needs_migration:
            return
        try:
            still_old = bool(getattr(self._store, "compat", True))
        except Exception:  # noqa: BLE001 -- a store that cannot say stays off
            return
        if not still_old:
            self.can_reconcile, self._needs_migration = True, False
            logger.info("[CRYSTAL] %s was migrated while open; reconcile re-enabled",
                        self.path)

    def bind_completion(self, complete: Optional[Callable[[str], str]]) -> None:
        """Decide reconciliations with an LLM (``LLMReconciler``); None = deterministic."""
        if complete is not None and not callable(complete):
            raise TypeError("complete must be callable(prompt) -> str")
        self._complete = complete

    def _reconciler(self) -> Any:
        from awm.reconcile import LLMReconciler, SlotReconciler  # type: ignore[import-not-found]

        return LLMReconciler(self._complete) if self._complete else SlotReconciler()

    def put(self, key: str, value: str, meta: dict) -> None:
        self._store.remember(self._scope, key, value, kind="crystal", meta=meta)

    def put_fact(self, fact: str, meta: dict) -> tuple[str, Optional[str]]:
        """Write one fact. Returns ``(route, degraded_reason)``.

        route = ``reconciled:<add|update|ignore>`` or ``legacy``. A reconcile that cannot
        run (old file, a malformed model reply) falls back to a legacy write so the fact
        is never lost, and the reason is returned for telemetry.
        """
        derived = derive_subject(fact)
        reason: Optional[str] = None
        self._recheck_reconcile()
        if derived is not None and self.can_reconcile:
            subject, value = derived
            try:
                d = self._reconcile(subject, value, meta)
                # A slot fact an older crystal wrote as a legacy row is the same slot:
                # superseded now (history keeps it), never recalled beside the new one.
                self._retire_legacy(subject)
                return f"reconciled:{d.action}", None
            except Exception as exc:  # noqa: BLE001 -- classified below, never swallowed
                name = type(exc).__name__
                if name == "NeedsMigration":
                    self.can_reconcile, self._needs_migration = False, True
                # ReconcileError includes the store REFUSING to supersede a value the
                # reconcile path does not own (an owner rule, a key-written fact): the
                # subject came from the fact's own text, which may quote a tool result.
                reason = f"awm-reconcile:{name}"
        elif derived is not None:
            # Say WHY: an older file (migrate it) is not an awm without reconcile.
            reason = ("awm-reconcile:NeedsMigration" if self._needs_migration
                      else "awm-reconcile:unsupported")
        self.put(fact_key(fact), fact, meta)
        return "legacy", reason

    def _reconcile(self, subject: str, value: str, meta: dict) -> Any:
        """``reconcile_and_remember`` at kind ``fact`` with the fact's meta (vec, ts)."""
        try:
            return self._store.reconcile_and_remember(
                self._scope, value, subject=subject, reconciler=self._reconciler(),
                kind="fact", meta=dict(meta or {}))
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            # An awm that predates kind/meta: the write still reconciles, the vector
            # and timestamp are dropped -- so say so.
            logger.warning("[CRYSTAL] awm reconcile takes no meta; vec/ts not stored")
            return self._store.reconcile_and_remember(self._scope, value, subject=subject,
                                                      reconciler=self._reconciler())

    def _retire_legacy(self, subject: str) -> int:
        """Forget legacy rows at EXACTLY this scope that state ``subject``'s slot."""
        n = 0
        for m in self._store.recall(self._scope, query=subject, kind="crystal",
                                    limit=10_000):
            if m.scope != str(self._scope):
                continue
            got = derive_subject(m.value)
            if got is not None and got[0] == subject and self._store.forget(self._scope,
                                                                              m.key):
                n += 1
        return n

    def scan(self, limit: int) -> list[tuple[str, str, dict]]:
        """Legacy crystal rows (``f:<hash>``) plus reconciled slot facts, as ``slot: value``.

        Both kinds are merged NEWEST first and only then cut to ``limit``: legacy rows
        first and a cut would hide every slot fact once a scope holds ``limit`` legacy
        ones -- including the fact written this turn.
        """
        rows = [(m.updated, (m.key, m.value, dict(m.meta or {})))
                for m in self._store.recall(self._scope, limit=limit, kind="crystal")]
        self._recheck_reconcile()
        if self.can_reconcile:
            for m in self._store.recall(self._scope, limit=limit, kind="fact"):
                meta = dict(m.meta or {})
                if meta.get("subject"):
                    rows.append((m.updated, (m.key, f"{m.key}: {m.value}", meta)))
        rows.sort(key=lambda t: -t[0])
        return [r for _u, r in rows[:limit]]

    def scan_landed(self, limit: int) -> list[tuple[str, str, dict]]:
        """Memory files landed by ``awm land`` at this scope or an ancestor
        (owner rules, project facts) -- any kind, tagged ``source: memory-file``.

        Filtered BEFORE the cut: taking the top ``limit`` rows of every kind first let
        this scope's own crystallized facts (5 compactions of 40) fill the cut, and an
        owner rule landed at the USER scope (weight 0.5) silently fell out of every
        turn's context -- ordinary output, or text an attacker got into it, crowding
        out the owner's rules. The landed rows alone are cut to ``limit``, nearest
        scope first; a cut that drops any is logged.
        """
        rows = [m for m in self._store.recall(self._scope, limit=10_000_000)
                if (m.meta or {}).get("source") == "memory-file"]
        if len(rows) > limit:
            logger.warning("[CRYSTAL] %d landed memory rows visible from %s; only the "
                           "nearest %d are scanned", len(rows), self._scope, limit)
        return [(m.key, m.value, dict(m.meta or {})) for m in rows[:limit]]

    def close(self) -> None:
        try:
            self._store.close()
        except Exception:  # noqa: BLE001
            pass


class AwgraphUnavailable(RuntimeError):
    """The graph could not be opened. ``str(exc)`` is the specific reason, in the
    token form CodeWorld's telemetry uses (``ModuleNotFoundError: ...`` when awgraph
    is not installed, ``no-index for <root>`` when it is but nothing was indexed)."""


class AwgraphIndex:
    """awgraph over a repository root, opened lazily once (hydrating a large index
    costs tens of seconds; measured 34.7 s on 387k chunks)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._graph: Any = None
        self._ok: bool | None = None
        #: Why the graph is unavailable ("" while unknown or available).
        self.reason = ""

    def probe(self) -> bool:
        """Cheap, no-hydration check that awgraph is importable at all. False sets
        ``reason`` and makes every later query fail fast with it."""
        if self._ok is False:
            return False
        try:
            import importlib.util

            found = importlib.util.find_spec("awgraph") is not None
            why = "ModuleNotFoundError: No module named 'awgraph'"
        except Exception as exc:  # noqa: BLE001 -- a broken finder is "not importable"
            found, why = False, f"{type(exc).__name__}: {exc}"
        if not found:
            self.reason, self._ok = why, False
        return found

    async def _open(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            from awgraph.cli import _open_graph  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[CRYSTAL] awgraph not importable: %s", self.reason)
            self._ok = False
            return False
        try:
            self._graph, self._ok = await _open_graph(self.root, build=False)
            if not self._ok:
                self.reason = f"no-index for {self.root} (run `awgraph index`)"
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[CRYSTAL] awgraph unavailable for %s: %s", self.root, exc)
            self._ok = False
        return bool(self._ok)

    async def query(self, text: str, k: int) -> list[dict]:
        if not await self._open():
            raise AwgraphUnavailable(self.reason or f"no-index for {self.root}")
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


def _gateway_embed_fallback() -> tuple[str, dict]:
    """The managed ``/v1/embeddings`` rung for a box with no MicroScheduler:
    ``(base_with_/v1, auth_headers)`` or ``("", {})`` when not enrolled."""
    try:
        from adk.embeddings import _gateway_rung
    except Exception:  # noqa: BLE001 -- no embeddings module means no managed rung
        return "", {}
    base, headers = _gateway_rung()
    if not base:
        return "", {}
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base, headers


def make_scheduler_embedder(url: str | None = None, model: str | None = None) -> Embedder:
    """An OpenAI-compatible ``/v1/embeddings`` embedder (MicroScheduler serves
    ``nomic-embed-text``; measured 2026-09-22 at https://127.0.0.1:8150/v1).

    With no explicit ``url``/``ADK_EMBED_URL`` the loopback scheduler is only the
    FIRST choice: a hosted tenant's box has nothing on :8150, so an unreachable
    scheduler falls back (once, then sticky) to the managed gateway rung that
    :mod:`adk.embeddings` uses, carrying the tenant bearer."""
    explicit = url or os.environ.get("ADK_EMBED_URL")
    base = (explicit or "https://127.0.0.1:8150/v1").rstrip("/")
    name = model or os.environ.get("ADK_EMBED_MODEL") or "nomic-embed-text"
    state: dict[str, Any] = {"base": base, "headers": {}, "fell_back": bool(explicit)}

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

        async def _run() -> list[Optional[list[float]]]:
            async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
                async def _one(text: str) -> Optional[list[float]]:
                    r = await client.post(f"{state['base']}/embeddings",
                                          json={"model": name, "input": text},
                                          headers=state["headers"])
                    r.raise_for_status()
                    data = r.json().get("data") or []
                    vec = data[0].get("embedding") if data else None
                    return [float(x) for x in vec] if vec else None

                return list(await asyncio.gather(*(_one(t) for t in texts)))

        try:
            return await _run()
        except httpx.TransportError:
            if state["fell_back"]:
                raise
            state["fell_back"] = True
            gw, headers = _gateway_embed_fallback()
            if not gw:
                raise
            logger.info("crystal: scheduler %s unreachable; embedding via gateway %s",
                        state["base"], gw)
            state["base"], state["headers"] = gw, headers
            return await _run()

    return _embed


def crystal_from_env(agent_name: str) -> "Optional[Crystal]":
    """Bind a crystal when ``ADK_CRYSTAL_SCOPE`` is set (``adk run --crystal SCOPE``).

    ``{agent}`` in the scope is replaced by the agent's name, so one server hosting
    several agents keeps one scope per agent. Unset, or an unbuildable crystal,
    returns None: the agent runs without it and the reason is logged.
    """
    raw = os.environ.get("ADK_CRYSTAL_SCOPE", "").strip()
    if not raw:
        return None
    name = agent_name or "assistant"
    if "{agent}" in raw and (not name.strip() or len(name) > 128
                             or _AGENT_SEGMENT_BAD.search(name)):
        # Refused, never rewritten into some other agent's name.
        if name not in BIND_TELEMETRY["refused"]:
            BIND_TELEMETRY["refused"].append(name)
        logger.warning("[CRYSTAL] agent name %r cannot be one scope segment (it holds "
                       "'*', ':' or a control character, or is empty or over 128 "
                       "characters); crystal not bound", name)
        return None
    scope = raw.replace("{agent}", name)
    try:
        return build_crystal(
            scope,
            db=os.environ.get("ADK_CRYSTAL_DB") or None,
            graph_root=os.environ.get("ADK_CRYSTAL_GRAPH_ROOT") or None,
            embed=os.environ.get("ADK_CRYSTAL_NO_EMBED", "").strip().lower()
            not in ("1", "true", "yes", "on"),
        )
    except Exception as exc:  # noqa: BLE001 -- a memory fault must not stop the agent
        logger.warning("[CRYSTAL] could not bind scope %s: %s", scope, exc)
        return None


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
        "recalled_memory": 0, "graph_hits": 0, "degraded": [], "planes": {},
        "reconciled": {"add": 0, "update": 0, "ignore": 0}, "legacy_writes": 0,
    })

    def __post_init__(self) -> None:
        # planes = bound AND not known to be unavailable. A bound graph whose awgraph
        # cannot be imported (or has no index, found at the first query) reads False:
        # True beside a "degraded: awgraph unavailable" told a host reading only
        # `planes` that the plane was there.
        self.telemetry["planes"] = {
            "awm": self.store is not None,
            "awgraph": self.graph is not None,
            "embed": self.embed is not None,
        }
        for plane, present in self.telemetry["planes"].items():
            if not present:
                self._degrade(f"{plane}:unbound")
        probe = getattr(self.graph, "probe", None)
        if callable(probe) and not probe():
            self.telemetry["planes"]["awgraph"] = False
            self._degrade(f"awgraph:{str(getattr(self.graph, 'reason', ''))[:200]}")

    def _degrade(self, reason: str) -> None:
        if reason not in self.telemetry["degraded"]:
            self.telemetry["degraded"].append(reason)
            logger.warning("[CRYSTAL] plane degraded: %s", reason)

    def bind_completion(self, complete: Optional[Callable[[str], str]]) -> bool:
        """Route reconciliation through an LLM. False when the store cannot reconcile."""
        bind = getattr(self.store, "bind_completion", None)
        if bind is None:
            self._degrade("awm-reconcile:store-has-no-completion-binding")
            return False
        bind(complete)
        return True

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
                put_fact = getattr(self.store, "put_fact", None)
                if put_fact is None:
                    self.store.put(fact_key(fact), fact, meta)
                    self.telemetry["legacy_writes"] += 1
                else:
                    route, reason = put_fact(fact, meta)
                    if reason:
                        self._degrade(reason)
                    if route.startswith("reconciled:"):
                        act = route.split(":", 1)[1]
                        self.telemetry["reconciled"][act] = (
                            self.telemetry["reconciled"].get(act, 0) + 1)
                    else:
                        self.telemetry["legacy_writes"] += 1
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
            # A vector from another embedder (a different dimension) cannot be
            # compared; keyword overlap still can, so fall back to it.
            if qvec is not None and vec and len(vec) == len(qvec):
                s = cosine(qvec, vec)
            else:
                s = keyword_score(message, value)
            ts = float(meta.get("ts", 0.0)) if isinstance(meta, dict) else 0.0
            scored.append((s, ts, value))
        # Best match first; a tie goes to the most recently established fact.
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [v for _s, _ts, v in scored[:limit]]

    async def recall_landed(self, message: str, *, limit: int = 6) -> list[str]:
        """Landed memory files that match this turn's message by keyword; [] when the
        store cannot scan landed memory (a fake, an old awm) or nothing matches."""
        scan = getattr(self.store, "scan_landed", None)
        if scan is None:
            return []
        try:
            rows = scan(SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awm-landed:{type(exc).__name__}")
            return []
        scored = [(keyword_score(message, v), k, v) for k, v, _m in rows]
        scored = [t for t in scored if t[0] > 0]
        scored.sort(key=lambda t: -t[0])
        return [f"{k}: {v.splitlines()[0][:240]}" for _s, k, v in scored[:limit]]

    async def recall_graph(self, message: str) -> list[dict]:
        if self.graph is None:
            return []
        try:
            return await self.graph.query(message, self.max_graph_hits)
        except AwgraphUnavailable as exc:
            # The specific reason: "not installed" and "no index here" have different
            # fixes, and CodeWorld reports the same brick the same way.
            self.telemetry["planes"]["awgraph"] = False
            self._degrade(f"awgraph:{str(exc)[:200]}")
            return []
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
        kept_mem = 0
        landed = await self.recall_landed(message)
        if landed:
            head = ("[MEMORY] Owner and project memory that matches this request. Treat "
                    "owner rules as standing instructions.")
            if used + len(head) + 1 <= self.max_chars:
                parts.append(head)
                used += len(head) + 1
                for m in landed:
                    line = f"- {m}"
                    if used + len(line) + 1 > self.max_chars:
                        break
                    parts.append(line)
                    used += len(line) + 1
                    kept_mem += 1
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
        self.telemetry["recalled_memory"] += kept_mem
        if kept_facts == 0 and kept_hits == 0 and kept_mem == 0:
            return ""
        return "\n".join(parts)


def build_crystal(scope: str, *, db: Path | str | None = None, graph_root: str | None = None,
                  embed_url: str | None = None, embed: bool = True,
                  complete: Optional[Callable[[str], str]] = None) -> Crystal:
    """The shipped composition: awm store + awgraph index + scheduler embedder, each
    bound only if it can be, the rest reported as degraded on the returned crystal."""
    store: Optional[FactStore] = None
    graph: Optional[GraphIndex] = None
    degraded: list[str] = []
    try:
        store = AwmFactStore(scope, db, complete=complete)
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"awm:{type(exc).__name__}")
    if graph_root:
        graph = AwgraphIndex(graph_root)
    embedder = make_scheduler_embedder(embed_url) if embed else None
    c = Crystal(scope=scope, store=store, graph=graph, embed=embedder)
    for d in degraded:
        c._degrade(d)
    return c

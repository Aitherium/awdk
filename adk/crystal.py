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

import asyncio
import hashlib
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

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
#: Vectors are stored rounded: 768 floats (nomic; 1024 in the aither-code-embed space)
#: at 4 decimals is ~5 KB (~7 KB) per fact.
VEC_DECIMALS = 4
#: Recall score floors. A fact scoring at or below these shares nothing with the request:
#: before the floor, rows scoring 0.0 still filled the block (only landed memory was
#: filtered), so an unrelated fact spent the turn's budget. Keyword overlap is a
#: normalized token-set ratio; cosine is over the bound embedder's vectors.
MIN_KEYWORD_SCORE = 0.05
#: With no query vector at all (embedder slow or down), every fact ranks by keyword:
#: only scores above this floor count, and at most KEYWORD_FALLBACK_LIMIT facts.
KEYWORD_FALLBACK_FLOOR = 0.15
KEYWORD_FALLBACK_LIMIT = 8
#: Measured 2026-10-14 on the daemon's store (scheduler embedder): "hi" scored 0.41 at
#: best against 86 facts (37 cleared 0.3, so a greeting recalled 20 unrelated facts and
#: the agent tried to post "Hi!" publicly); facts the question was about scored 0.46-0.57.
MIN_COSINE_SCORE = 0.45
#: How long one turn waits for the code graph. A cold awgraph open hydrates the whole
#: index (measured 37.3 s for a probe and ~55 s before the first token of a real turn on
#: a 493k-chunk index); past this the turn goes on without graph hits, marked degraded,
#: and the open keeps running in the background for the next turn.
#: Kept well under the turn's crystal stage budget (``CRYSTAL_STAGE_S``): at 3.0 s, equal
#: to the stage, a slow graph query always lost the race and the stage dropped the
#: facts it had already recalled with it (measured 2026-10-14: every live turn).
GRAPH_WAIT_S = 1.5
#: What a turn waits for the whole crystal block (facts + memory + graph).
CRYSTAL_STAGE_S = GRAPH_WAIT_S + 2.0
#: The graph wait for a CODE request when the graph runs in its own process
#: (``ADK_AWGRAPH_WORKER``): a warm query takes ~4.7 s there (measured 2026-10-14) and,
#: out of process, waiting for it blocks nothing else in the turn.
CODE_GRAPH_WAIT_S = 5.0
#: How long recall waits for the query's embedding before ranking facts by keyword.
EMBED_WAIT_S = 1.0
#: Env var naming EXTRA scopes recall reads (comma-separated). Read from the process
#: environment only -- the daemon's own config -- never from a request: a scope taken
#: from a payload would let one caller read another tenant's memory.
READ_SCOPES_ENV = "ADK_CRYSTAL_READ_SCOPES"
#: How long a scan of landed memory files is reused (``ADK_CRYSTAL_LANDED_CACHE_S``).
try:
    LANDED_CACHE_S = float(os.environ.get("ADK_CRYSTAL_LANDED_CACHE_S") or 300)
except ValueError:
    LANDED_CACHE_S = 300.0

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
        # check_same_thread=False lets the per-turn scans run in a worker thread
        # (``offload_reads``); every use of the handle is serialized by ``_lock``.
        self._lock = threading.RLock()
        #: True when scan/scan_landed may run off the event loop (Crystal reads this).
        self.offload_reads = False
        try:
            self._store = MemoryStore(path, auto_migrate=False, check_same_thread=False)
            self.offload_reads = True
        except TypeError:  # an awm that predates one of the flags
            try:
                self._store = MemoryStore(path, auto_migrate=False)
            except TypeError:
                self._store = MemoryStore(path)
        self.path = path
        self._complete = complete
        #: Feature detection, not a version compare: the method must exist AND work on
        #: this file (a compat-mode file raises NeedsMigration and flips this off).
        self.can_reconcile = callable(getattr(self._store, "reconcile_and_remember", None))
        #: True while can_reconcile is off ONLY because the file is an older schema: a
        #: migration run by another process while this handle is open turns it back on.
        self._needs_migration = False
        #: limit -> (monotonic time, rows). Landed rows change only on `awm land`;
        #: reading every row of every read scope cost ~10 s per turn on a 2.5k-row
        #: project scope (measured 2026-10-14), past the turn's 3 s crystal budget.
        self._landed_cache: dict = {}

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
        with self._lock:
            self._store.remember(self._scope, key, value, kind="crystal", meta=meta)

    def put_fact(self, fact: str, meta: dict) -> tuple[str, Optional[str]]:
        """Write one fact. Returns ``(route, degraded_reason)``.

        route = ``reconciled:<add|update|ignore>`` or ``legacy``. A reconcile that cannot
        run (old file, a malformed model reply) falls back to a legacy write so the fact
        is never lost, and the reason is returned for telemetry.
        """
        with self._lock:
            return self._put_fact(fact, meta)

    def _put_fact(self, fact: str, meta: dict) -> tuple[str, Optional[str]]:
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

    def _read_scopes(self) -> list:
        """The write scope first, then each configured read scope (``read_scopes``,
        set by ``build_crystal`` from the daemon environment), deduplicated."""
        out, seen = [self._scope], {str(self._scope)}
        for sc in getattr(self, "read_scopes", None) or ():
            if str(sc) not in seen:
                seen.add(str(sc))
                out.append(sc)
        return out

    def scan(self, limit: int) -> list[tuple[str, str, dict]]:
        """Legacy crystal rows (``f:<hash>``) plus ``kind=fact`` rows as ``key: value``,
        from the write scope AND every read scope (each with its ancestors).

        Per scope, both kinds are merged NEWEST first and only then cut to ``limit``:
        legacy rows first and a cut would hide every slot fact once a scope holds
        ``limit`` legacy ones -- including the fact written this turn. Each scope gets
        its own ``limit`` so a large read scope never crowds out this task's own facts.

        ``kind=fact`` rows are read whether or not this file can RECONCILE (reading needs
        no migration) and whether or not they carry a subject: measured 2026-10-11, the
        platform facts visible to the daemon were all subject-less, so all were dropped.
        """
        with self._lock:
            return self._scan(limit)

    def _scan(self, limit: int) -> list[tuple[str, str, dict]]:
        merged: list[tuple[str, str, dict]] = []
        seen: set[tuple[str, str]] = set()
        for sc in self._read_scopes():
            rows = [(m.updated, m.scope, (m.key, m.value, dict(m.meta or {})))
                    for m in self._store.recall(sc, limit=limit, kind="crystal")]
            for m in self._store.recall(sc, limit=limit, kind="fact"):
                # A landed memory file with no `type` is kind=fact too; it belongs
                # to scan_landed / the [MEMORY] section, never whole in the facts.
                if (m.meta or {}).get("source") == "memory-file":
                    continue
                rows.append((m.updated, m.scope, (m.key, f"{m.key}: {m.value}",
                                                  dict(m.meta or {}))))
            rows.sort(key=lambda t: -t[0])
            for _u, row_scope, row in rows[:limit]:
                if (row_scope, row[0]) in seen:  # an ancestor two read scopes share
                    continue
                seen.add((row_scope, row[0]))
                merged.append(row)
        self._recheck_reconcile()
        return merged

    def scan_landed(self, limit: int) -> list[tuple[str, str, dict]]:
        """Memory files landed by ``awm land`` at this scope or an ancestor
        (owner rules, project facts) -- any kind, tagged ``source: memory-file``.

        Filtered BEFORE the cut: taking the top ``limit`` rows of every kind first let
        this scope's own crystallized facts (5 compactions of 40) fill the cut, and an
        owner rule landed at the USER scope (weight 0.5) silently fell out of every
        turn's context -- ordinary output, or text an attacker got into it, crowding
        out the owner's rules. The landed rows alone are cut to ``limit``, nearest
        scope first; a cut that drops any is logged.

        Read scopes (``read_scopes``) are unioned after the write scope: an owner's
        project memory lands at the owner's scope, a SIBLING of the agent's, which the
        write scope alone can never see (measured 2026-10-11: 0 landed rows visible).
        """
        now = time.monotonic()
        with self._lock:
            hit = self._landed_cache.get(limit)
            if hit is not None and now - hit[0] < LANDED_CACHE_S:
                return list(hit[1])
            rows = self._scan_landed(limit)
            self._landed_cache[limit] = (now, rows)
            return list(rows)

    def _scan_landed(self, limit: int) -> list[tuple[str, str, dict]]:
        rows: list = []
        seen: set[tuple[str, str]] = set()
        for sc in self._read_scopes():
            for m in self._store.recall(sc, limit=10_000_000):
                if (m.meta or {}).get("source") != "memory-file":
                    continue
                if (m.scope, m.key) in seen:
                    continue
                seen.add((m.scope, m.key))
                rows.append(m)
        if len(rows) > limit:
            logger.warning("[CRYSTAL] %d landed memory rows visible from %s; only the "
                           "nearest %d are scanned", len(rows), self._scope, limit)
        return [(m.key, m.value, dict(m.meta or {})) for m in rows[:limit]]

    def close(self) -> None:
        try:
            with self._lock:
                self._store.close()
        except Exception:  # noqa: BLE001
            pass


async def _no_hits() -> list[dict]:
    return []


def _worker_mode() -> bool:
    """Open the code graph in a child process (``ADK_AWGRAPH_WORKER=1``; the daemon sets
    it). In-process, awgraph's pure-Python load and scorer hold the GIL for whole turns."""
    return os.environ.get("ADK_AWGRAPH_WORKER", "").strip().lower() in ("1", "true", "yes")


def _worker_argv(root: str) -> "list | None":
    """The worker command; None = the default (``python -m adk.graph_worker <root>``)."""
    return None


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
        self._query_lock = threading.Lock()
        self._hydrate_lock = threading.Lock()

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

    @property
    def is_warm(self) -> bool:
        """True once the index is hydrated and usable."""
        return self._ok is True

    async def _open(self) -> bool:
        """Hydrate once per process, however many callers ask at the same time.

        Every concurrent caller awaits ONE shielded open task: two turns arriving while
        the index is cold used to start two 37 s hydrations of the same index, and a
        caller that stops waiting (the recall timeout) must not cancel the open the
        next turn will use.
        """
        if self._ok is not None:
            return self._ok
        loop = asyncio.get_running_loop()
        task = getattr(self, "_opening", None)
        if task is None or task.get_loop() is not loop or task.done():
            task = loop.create_task(self._hydrate())
            self._opening = task
        return await asyncio.shield(task)

    async def _hydrate(self) -> bool:
        if self._ok is not None:
            return self._ok
        if _worker_mode():
            return await self._hydrate_worker()
        try:
            from awgraph.cli import _open_graph  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[CRYSTAL] awgraph not importable: %s", self.reason)
            self._ok = False
            return False
        try:
            # _open_graph is async in name only: the chunk-cache load and the
            # embedding hydrate are synchronous (~37 s). On the loop they froze every
            # turn and GRAPH_WAIT_S could never fire, so it runs in a worker thread.
            # ONE hydration per process. The single-flight task above is per event loop,
            # and the server's warm-up runs on a worker thread's own loop: measured
            # 2026-10-14, the warm-up and the first turn each hydrated the same 495k-chunk
            # index at once (two threads, ~3 GB RSS, the GIL starved for minutes).
            root = self.root

            def _load() -> Any:
                with self._hydrate_lock:
                    if self._ok is None:
                        graph, ok = asyncio.run(  # blocking-ok: runs in a worker thread
                            _open_graph(root, build=False))
                        self._graph, self._ok = graph, ok
                    return self._graph, self._ok

            self._graph, self._ok = await asyncio.to_thread(_load)
            if not self._ok:
                self.reason = f"no-index for {self.root} (run `awgraph index`)"
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[CRYSTAL] awgraph unavailable for %s: %s", self.root, exc)
            self._ok = False
        return bool(self._ok)

    async def _hydrate_worker(self) -> bool:
        """Open the graph in a child process (``adk.graph_worker``), once per process."""
        from adk.graph_worker import GraphWorkerClient

        def _start() -> Any:
            with self._hydrate_lock:
                if self._ok is None:
                    client = GraphWorkerClient(self.root, argv=_worker_argv(self.root))
                    ok = client.start()
                    self._graph, self._ok = client, ok
                    if not ok:
                        self.reason = client.reason or f"no-index for {self.root}"
                        client.close()
                return self._graph, self._ok

        try:
            await asyncio.to_thread(_start)
        except Exception as exc:  # noqa: BLE001 -- reported through telemetry
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[CRYSTAL] graph worker failed for %s: %s", self.root, exc)
            self._ok = False
        return bool(self._ok)

    async def query(self, text: str, k: int) -> list[dict]:
        if not await self._open():
            raise AwgraphUnavailable(self.reason or f"no-index for {self.root}")
        from adk.graph_worker import GraphWorkerClient

        if isinstance(self._graph, GraphWorkerClient):
            client = self._graph
            try:
                return await asyncio.to_thread(client.query, text, k, GRAPH_WAIT_S + 1.0)
            except RuntimeError as exc:
                if not client.alive:  # the worker died: the next open restarts it
                    self._graph, self._ok, self._opening = None, None, None
                raise AwgraphUnavailable(str(exc)) from exc
        # hybrid_query is async in name only too: a warm query over ~495k chunks ran
        # 4-12 s of numpy on the loop and froze it up to 2 s at a time (measured
        # 2026-10-14), so every OTHER recall stage's timer fired late and the whole
        # turn context read "degraded: TimeoutError". It runs in a worker thread,
        # one query at a time (the graph object is not known to be thread-safe).
        graph = self._graph

        def _run() -> Any:
            with self._query_lock:
                query = graph.hybrid_query(text, max_results=k)
                return asyncio.run(query)  # blocking-ok: runs in a worker thread

        chunks = await asyncio.to_thread(_run)
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
    """An OpenAI-compatible ``/v1/embeddings`` embedder (MicroScheduler; measured
    2026-09-22 at https://127.0.0.1:8150/v1).

    The model and width follow the active space (:mod:`adk.embeddings` is the
    authority): ``nomic-embed-text`` 768-d, or ``aither-code-embed`` 1024-d. In the
    code-embed space a vector of any other width is refused (``None``, never stored),
    exactly as ``adk.embeddings._width_ok`` refuses it, and a leftover nomic
    ``ADK_EMBED_MODEL`` is stale config, not a choice.

    With no explicit ``url``/``ADK_EMBED_URL`` the loopback scheduler is only the
    FIRST choice: a hosted tenant's box has nothing on :8150, so an unreachable
    scheduler falls back (once, then sticky) to the managed gateway rung that
    :mod:`adk.embeddings` uses, carrying the tenant bearer."""
    from adk import embeddings as space  # read at call time: tests reload the space

    explicit = url or os.environ.get("ADK_EMBED_URL")
    base = (explicit or "https://127.0.0.1:8150/v1").rstrip("/")
    name = model or os.environ.get("ADK_EMBED_MODEL") or space.CANONICAL_MODEL
    code_embed = space.EMBED_SPACE == space.SPACE_CODE_EMBED
    if code_embed and space._is_nomic_model(name):
        name = space.CANONICAL_MODEL
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
                                          json={"model": name,
                                                "input": space._truncate(text)},
                                          headers=state["headers"])
                    r.raise_for_status()
                    data = r.json().get("data") or []
                    vec = data[0].get("embedding") if data else None
                    if not vec:
                        return None
                    if code_embed and len(vec) != space.CANONICAL_DIM:
                        logger.warning(
                            "crystal: %s answered a %d-d vector in the %s space (want "
                            "%d); refused", state["base"], len(vec), space.EMBED_SPACE,
                            space.CANONICAL_DIM)
                        return None
                    return [float(x) for x in vec]

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
    #: step -> monotonic start, for the steps of a recall still running. A turn whose
    #: crystal stage times out reports these, so the trace names the slow step
    #: instead of a bare TimeoutError (2026-10-14: that bare error cost hours).
    open_steps: dict = field(default_factory=dict)
    max_graph_hits: int = 6
    telemetry: dict = field(default_factory=lambda: {
        "writes": 0, "compactions": 0, "recalls": 0, "recalled_facts": 0,
        "recalled_memory": 0, "graph_hits": 0, "degraded": [], "planes": {},
        "reconciled": {"add": 0, "update": 0, "ignore": 0}, "legacy_writes": 0,
    })
    # What the LAST recall_block put in front of the model, for the turn trace:
    # {"facts": int, "memory": int, "graph_hits": int, "chars": int}. {} until the
    # first recall. Telemetry counters are cumulative and cannot say what ONE turn saw.
    last_recall: dict = field(default_factory=dict)

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

    def _open_step(self, step: str) -> None:
        self.open_steps[step] = time.monotonic()

    def _close_step(self, step: str) -> None:
        self.open_steps.pop(step, None)

    def describe_open_steps(self) -> str:
        """``"embed 3.1s, scan 0.4s"`` for the recall steps still running ('' = none)."""
        now = time.monotonic()
        return ", ".join(f"{k} {now - t:.1f}s" for k, t in
                         sorted(self.open_steps.items(), key=lambda kv: kv[1]))

    async def _read(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a store read off the event loop when the store says it may: a turn's
        scans cover every read scope (sqlite + a JSON parse of every row's vector,
        measured 283 ms on 1,400 rows) and on the loop they stalled every other turn."""
        step = f"read:{getattr(fn, '__name__', 'store')}"
        self._open_step(step)
        try:
            if getattr(self.store, "offload_reads", False) is True:
                out = await asyncio.to_thread(fn, *args)
            else:
                out = fn(*args)
        except asyncio.CancelledError:
            raise  # left open on purpose: the turn's timeout report names it
        except BaseException:
            self._close_step(step)
            raise
        self._close_step(step)
        return out

    async def _vectors(self, texts: list[str]) -> list[Optional[list[float]]]:
        if self.embed is None or not texts:
            return [None] * len(texts)
        self._open_step("embed")
        try:
            vecs = await self.embed(texts)
        except asyncio.CancelledError:
            raise  # left open on purpose: the turn's timeout report names it
        except Exception as exc:  # noqa: BLE001
            self._close_step("embed")
            self._degrade(f"embed:{type(exc).__name__}")
            return [None] * len(texts)
        self._close_step("embed")
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

    async def recall_facts(self, message: str, *, limit: int = 20,
                           stats: Optional[dict] = None) -> list[str]:
        """The facts nearest ``message``, best first. Rows at or below the score floor
        (``MIN_COSINE_SCORE`` / ``MIN_KEYWORD_SCORE``) share nothing with the request
        and are dropped; ``stats["dropped"]``, when a dict is passed, counts them."""
        if self.store is None:
            self._degrade("awm:unbound")
            return []
        try:
            rows = await self._read(self.store.scan, SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awm:{type(exc).__name__}")
            return []
        if not rows:
            return []
        qvec = None
        try:
            from adk.embeddings import embed_breaker_open, trip_embed_breaker
        except Exception:  # noqa: BLE001 -- no shared breaker: wait as before
            embed_breaker_open, trip_embed_breaker = (lambda: False), (lambda reason: None)
        if self.embed is not None and embed_breaker_open():
            self._degrade("embed:breaker-open")
        elif self.embed is not None:
            # The embedder shares the GPU with the chat models; measured 2026-10-14 it
            # answered one query in 3-10 s, past the whole crystal stage, and the turn
            # lost every fact. Past EMBED_WAIT_S the facts rank by keyword instead.
            try:
                qvec = (await asyncio.wait_for(self._vectors([message]), EMBED_WAIT_S))[0]
            except asyncio.TimeoutError:
                self._degrade("embed:slow")
                trip_embed_breaker(f"crystal query embedding over {EMBED_WAIT_S:.1f}s")
        scored: list[tuple[float, float, str]] = []
        dropped = 0
        if qvec is None:
            # Keyword ranking is the fallback, and a weak one: one shared word ranks a
            # fact. It keeps only clear matches, and few of them.
            limit = min(limit, KEYWORD_FALLBACK_LIMIT)
        kw_floor = MIN_KEYWORD_SCORE if qvec is not None else KEYWORD_FALLBACK_FLOOR
        for _key, value, meta in rows:
            vec = meta.get("vec") if isinstance(meta, dict) else None
            # A vector from another embedder (a different dimension) cannot be
            # compared; keyword overlap still can, so fall back to it.
            if qvec is not None and vec and len(vec) == len(qvec):
                s, floor = cosine(qvec, vec), MIN_COSINE_SCORE
            else:
                s, floor = keyword_score(message, value), kw_floor
            if s <= floor:
                dropped += 1
                continue
            try:
                ts = float(meta.get("ts", 0.0)) if isinstance(meta, dict) else 0.0
            except (TypeError, ValueError):
                ts = 0.0  # a foreign row's ts is not ours to trust
            scored.append((s, ts, value))
        # Best match first; a tie goes to the most recently established fact.
        scored.sort(key=lambda t: (-t[0], -t[1]))
        if stats is not None:
            stats["dropped"] = stats.get("dropped", 0) + dropped
        return [v for _s, _ts, v in scored[:limit]]

    async def recall_landed(self, message: str, *, limit: int = 6) -> list[str]:
        """Landed memory files that match this turn's message by keyword; [] when the
        store cannot scan landed memory (a fake, an old awm) or nothing matches."""
        scan = getattr(self.store, "scan_landed", None)
        if scan is None:
            return []
        try:
            rows = await self._read(scan, SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awm-landed:{type(exc).__name__}")
            return []
        scored = [(keyword_score(message, v), k, v) for k, v, _m in rows]
        scored = [t for t in scored if t[0] > 0]
        scored.sort(key=lambda t: -t[0])
        return [f"{k}: {v.splitlines()[0][:240]}" for _s, k, v in scored[:limit]]

    async def recall_graph(self, message: str, wait: "float | None" = None) -> list[dict]:
        """Code-graph hits for ``message`` with ABSOLUTE paths, or [] (degraded).

        Bounded by ``GRAPH_WAIT_S``: the query runs as a shielded task, so a cold index
        that is still hydrating returns [] with ``awgraph:degraded:cold`` and keeps
        hydrating for the next turn instead of blocking this one for ~40-55 s.
        """
        if self.graph is None:
            return []
        cold = getattr(self.graph, "is_warm", True) is False
        try:
            task = asyncio.ensure_future(self.graph.query(message, self.max_graph_hits))
            task.add_done_callback(_reap_background)
            try:
                hits = await asyncio.wait_for(asyncio.shield(task),
                                              GRAPH_WAIT_S if wait is None else wait)
            except asyncio.TimeoutError:
                self._degrade("awgraph:degraded:cold" if cold else "awgraph:timeout")
                self._graph_degraded = "cold" if cold else "timeout"
                return []
            root = getattr(self.graph, "root", "") or ""
            out: list[dict] = []
            for h in hits or []:
                h = dict(h)
                path = str(h.get("path") or "")
                # Relative paths were relative to the GRAPH root, not the agent's cwd:
                # the model opened them from its own cwd and missed (measured 10-11).
                if path and root and not os.path.isabs(path):
                    h["path"] = os.path.normpath(os.path.join(root, path))
                out.append(h)
            return out
        except AwgraphUnavailable as exc:
            # The specific reason: "not installed" and "no index here" have different
            # fixes, and CodeWorld reports the same brick the same way.
            self.telemetry["planes"]["awgraph"] = False
            self._degrade(f"awgraph:{str(exc)[:200]}")
            return []
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awgraph:{type(exc).__name__}")
            return []

    async def recall_block(self, message: str, exclude: "tuple | list" = (),
                           max_chars: "int | None" = None, graph: bool = True,
                           graph_wait: "float | None" = None) -> str:
        """The system block for a turn: facts + graph hits under ``max_chars``. "" = nothing.

        ``exclude``: texts the caller already shows the model (history, another block);
        a recalled fact whose normalized text equals one of their lines, or appears inside
        one of them, is a duplicate and skipped. ``max_chars``: this call's cap,
        overriding ``self.max_chars`` when given (None = the instance cap). Up to half
        the cap is reserved for code-graph hits: measured 2026-10-11, a full fact block
        left no room for the [CODE GRAPH] lines, which are the ones that save file reads.
        Fails open: a plane that raises is reported in ``telemetry["degraded"]`` and
        contributes nothing.
        """
        # The cap is a parameter, not a temporary self.max_chars swap: two concurrent
        # turns on one crystal would otherwise read each other's cap.
        cap = int(max_chars) if max_chars is not None and max_chars > 0 else self.max_chars
        return await self._recall_block(message, cap, exclude, graph, graph_wait)

    async def _recall_block(self, message: str, cap: int,
                            exclude: "Iterable[str]" = (), graph: bool = True,
                            graph_wait: "float | None" = None) -> str:
        self.telemetry["recalls"] += 1
        self.open_steps.clear()  # a cancelled recall's steps are history now
        self._graph_degraded = ""
        stats: dict = {"dropped": 0}
        seen_lines, seen_blob = _exclusion_index(exclude)
        # Concurrently: each waits on a different slow plane (the embedder, the graph),
        # and one after the other they overran the turn's crystal stage under load
        # (2026-10-14), which then dropped the facts too.
        facts, hits = await asyncio.gather(self.recall_facts(message, stats=stats),
                                           self.recall_graph(message, graph_wait) if graph
                                           else _no_hits())
        graph_lines: list[str] = []
        for h in hits:
            sig = (h.get("signature") or h.get("name") or "").strip()
            graph_lines.append(f"- {h.get('path', '')}:{h.get('line', 0)} {sig}"[:200])
        graph_head = ("[CODE GRAPH] Symbols the code graph matched for this request. Open "
                      "these before any broad file read.")
        # Reserve what the graph section will actually use, up to half the cap: no graph
        # hits means the facts may use the whole block.
        reserve = 0
        if graph_lines:
            reserve = min(cap // 2, len(graph_head) + 1
                          + sum(len(ln) + 1 for ln in graph_lines))
        text_cap = cap - reserve
        parts: list[str] = []
        used = 0
        kept_facts = 0
        fact_lines: list[str] = []
        for f in facts:
            if _is_excluded(f, seen_lines, seen_blob):
                stats["dropped"] += 1
                continue
            fact_lines.append(f"- {f}")
        if fact_lines:
            head = ("[CRYSTAL] Facts this agent already established for this task, recalled "
                    "from memory. Build on them; re-verify only what a tool result contradicts.")
            parts.append(head)
            used += len(head)
            for line in fact_lines:
                if used + len(line) + 1 > text_cap:
                    continue  # one oversized fact must not starve the shorter rest
                parts.append(line)
                used += len(line) + 1
                kept_facts += 1
            stats["dropped"] += len(fact_lines) - kept_facts
            if kept_facts == 0:
                parts.pop()
                used = 0
        kept_mem = 0
        landed = [m for m in await self.recall_landed(message)
                  if not _is_excluded(m, seen_lines, seen_blob)]
        if landed:
            head = ("[MEMORY] Owner and project memory that matches this request. Treat "
                    "owner rules as standing instructions.")
            if used + len(head) + 1 <= text_cap:
                parts.append(head)
                used += len(head) + 1
                for m in landed:
                    line = f"- {m}"
                    if used + len(line) + 1 > text_cap:
                        break
                    parts.append(line)
                    used += len(line) + 1
                    kept_mem += 1
                if kept_mem == 0:
                    parts.pop()
                    used -= len(head) + 1
        kept_hits = 0
        if graph_lines and used + len(graph_head) + 1 <= cap:
            parts.append(graph_head)
            used += len(graph_head) + 1
            for line in graph_lines:
                if used + len(line) + 1 > cap:
                    break
                parts.append(line)
                used += len(line) + 1
                kept_hits += 1
            if kept_hits == 0:
                parts.pop()
                used -= len(graph_head) + 1
        self.telemetry["recalled_facts"] += kept_facts
        self.telemetry["graph_hits"] += kept_hits
        self.telemetry["recalled_memory"] += kept_mem
        any_kept = bool(kept_facts or kept_hits or kept_mem)
        self.last_recall = {"facts": kept_facts, "memory": kept_mem, "graph_hits": kept_hits,
                            "dropped": stats["dropped"],
                            "degraded": list(self.telemetry.get("degraded", [])),
                            "graph": self._graph_degraded or ("ok" if hits else "none"),
                            "chars": used if any_kept else 0}
        if not any_kept:
            return ""
        return "\n".join(parts)


def _norm_line(text: str) -> str:
    return re.sub(r"\s+", " ", _BULLET.sub("", text or "").strip().lower())


def _exclusion_index(exclude: "Iterable[str]") -> tuple[set, str]:
    """(normalized lines, normalized blob) of everything the caller already shows."""
    lines: set = set()
    blobs: list[str] = []
    for text in exclude or ():
        if not isinstance(text, str) or not text:
            continue
        for raw in text.splitlines():
            n = _norm_line(raw)
            if n:
                lines.add(n)
        blobs.append(re.sub(r"\s+", " ", text.lower()))
    return lines, "\n".join(blobs)


def _is_excluded(fact: str, lines: set, blob: str) -> bool:
    n = _norm_line(fact)
    if not n:
        return False
    # Substring only for fact-sized text: a 6-char line would match half the history.
    return n in lines or (len(n) >= FACT_MIN_CHARS and n in blob)


def _reap_background(task: "asyncio.Future") -> None:
    """Retrieve a background graph task's outcome so a failure is logged, not lost."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, AwgraphUnavailable):
        logger.warning("[CRYSTAL] background graph query failed: %s: %s",
                       type(exc).__name__, exc)


# ── one hydrated code graph per process ─────────────────────────────────────

#: One ``AwgraphIndex`` per absolute root, shared by every crystal in the process:
#: each agent name used to build its own and pay its own cold load (measured 37-55 s
#: per agent on a 493k-chunk index).
_GRAPH_CACHE: dict = {}


def shared_graph(root: "str | None" = None) -> "Optional[AwgraphIndex]":
    """The process-wide ``AwgraphIndex`` for ``root`` (default: the env
    ``ADK_CRYSTAL_GRAPH_ROOT``), created on first use. None when no root is known.
    This is the handle local graph tools use, so they share the crystal's hydration."""
    root = root or os.environ.get("ADK_CRYSTAL_GRAPH_ROOT") or ""
    if not root:
        return None
    key = os.path.normcase(os.path.abspath(root))
    idx = _GRAPH_CACHE.get(key)
    if idx is None:
        idx = AwgraphIndex(root)
        _GRAPH_CACHE[key] = idx
    return idx


def warm(root: "str | None" = None) -> int:
    """Start hydrating the code graph in the background (call at server startup).

    ``root`` = one root; None = every cached root plus ``ADK_CRYSTAL_GRAPH_ROOT``.
    Returns how many opens were scheduled. Needs a running event loop; without one it
    logs and returns 0 -- warming is an optimisation, never a startup failure.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("[CRYSTAL] warm(): no running event loop; graph opens on first use")
        return 0
    if root:
        one = shared_graph(root)
        targets = [one] if one is not None else []
    else:
        env_idx = shared_graph(None)
        targets = list(_GRAPH_CACHE.values())
        if env_idx is not None and env_idx not in targets:
            targets.append(env_idx)
    n = 0
    for idx in targets:
        try:
            if idx._ok is not None or not idx.probe():
                continue
            loop.create_task(idx._open()).add_done_callback(_reap_background)
            n += 1
        except Exception as exc:  # noqa: BLE001 -- warming must never fail startup
            logger.warning("[CRYSTAL] warm(%s) failed: %s", idx.root, exc)
    return n


def _env_read_scopes() -> list:
    """``ADK_CRYSTAL_READ_SCOPES`` from the process environment, comma-separated."""
    raw = os.environ.get(READ_SCOPES_ENV, "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def build_crystal(scope: str, *, db: Path | str | None = None, graph_root: str | None = None,
                  embed_url: str | None = None, embed: bool = True,
                  complete: Optional[Callable[[str], str]] = None,
                  read_scopes: "Optional[Iterable[str]]" = None) -> Crystal:
    """The shipped composition: awm store + awgraph index + scheduler embedder, each
    bound only if it can be, the rest reported as degraded on the returned crystal.

    ``read_scopes``: extra scopes recall reads (writes stay at ``scope``). None reads
    ``ADK_CRYSTAL_READ_SCOPES`` from the process environment; callers must never pass
    a value that came from a request. An unparseable scope is skipped and reported.
    The graph index is the process-wide one for ``graph_root`` (``shared_graph``).
    """
    store: Optional[FactStore] = None
    graph: Optional[GraphIndex] = None
    degraded: list[str] = []
    try:
        store = AwmFactStore(scope, db, complete=complete)
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"awm:{type(exc).__name__}")
    if store is not None:
        wanted = list(read_scopes) if read_scopes is not None else _env_read_scopes()
        parsed = []
        for raw in wanted:
            try:
                from awm.scope import Scope  # type: ignore[import-not-found]

                parsed.append(Scope.parse(str(raw).strip()))
            except Exception as exc:  # noqa: BLE001 -- one bad entry never unbinds awm
                degraded.append(f"awm-read-scope:{type(exc).__name__}")
                logger.warning("[CRYSTAL] read scope %r refused: %s", raw, exc)
        store.read_scopes = parsed  # type: ignore[attr-defined]
        if parsed:
            logger.info("[CRYSTAL] recall reads %s plus %d read scope(s)", scope, len(parsed))
    if graph_root:
        graph = shared_graph(graph_root)
    embedder = make_scheduler_embedder(embed_url) if embed else None
    c = Crystal(scope=scope, store=store, graph=graph, embed=embedder)
    for d in degraded:
        c._degrade(d)
    return c

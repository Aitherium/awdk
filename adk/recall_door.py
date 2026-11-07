"""adk.recall_door -- the one door every expensive producer knocks on FIRST.

The same question should never be paid for twice. Before a producer spends
model calls it calls :func:`lookup`; afterwards, only if the run actually
produced something, it calls :func:`record`. Content is addressed by its
SHA-256 so storing one output twice costs one object.

    from adk.recall_door import lookup, record, verify_outcome

    got = lookup("forge", task, scope=tenant)
    if got.status == "hit":          # reuse with provenance, zero model calls
        return got.output
    seed = got.output if got.status == "seed" else ""

    output, evidence = run_the_work(task, seed=seed)
    ok, why = verify_outcome(output, artifacts=evidence)
    if ok:
        record("forge", task, output, agent="demiurge", scope=tenant)

Statuses
--------
``hit``   an exact or near-duplicate prior request with a VERIFIED output.
``seed``  a prior example worth starting from, not a substitute for the work.
``miss``  nothing useful stored; investigate normally.

Nothing unverified is ever returned as a ``hit``. A producer that reports
success without producing an artifact, an action, or non-trivial output is
recorded as ``unverified`` (for audit) and stays invisible to lookups.

Store
-----
``AITHER_RECALL_DIR`` (default ``~/.aither/recall``). A SQLite index plus a
content-addressed ``objects/<aa>/<sha256>`` tree. ``prune()`` decays unused
records, dedupes near-duplicates and removes unreferenced objects, writing a
tombstone for every removed record so provenance survives the delete.

CLI::

    python -m adk.recall_door --stats
    python -m adk.recall_door --lookup forge "add retry to the payment client"
    python -m adk.recall_door --prune --ttl-days 30
    python -m adk.recall_door --self-test
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "RecallHit",
    "Status",
    "default_root",
    "lookup",
    "record",
    "verify_outcome",
    "stats",
    "prune",
    "recent",
    "log_event",
]

HIT_THRESHOLD = 0.72
SEED_THRESHOLD = 0.30
DEFAULT_TTL_DAYS = 45
MIN_VERIFIED_CHARS = 24

# A "completed" whose only tool use is reads has not produced anything. These
# are the read-only verbs the runtime exposes; a side-effecting call (write,
# mail, deploy, dispatch) is evidence of an ACTION.
_READ_ONLY_TOOLS = frozenset({
    "list_directory", "list_dir", "ls", "read_file", "read", "cat", "grep",
    "search", "find", "glob", "head", "tail", "stat", "exists", "diff",
    "web_search", "webfetch", "fetch", "scan", "inspect", "status", "health",
})

_CANNED_FAILURE = re.compile(
    r"(the ai backend is temporarily unavailable|i (?:cannot|can't|could not) "
    r"(?:complete|do|access)|no (?:output|result|answer) (?:was )?produced"
    # A runtime failure handed back as the session's text. These were recorded as
    # VERIFIED and then replayed as a hit for every retry of the same ask, so a task
    # that failed once on a timeout failed forever in 20 s with 0 model calls.
    r"|\[agent failed\]|backend_failure|llm call timed out"
    r"|the language model took too long to respond"
    r"|the agent ended without a result verdict"
    # A forge run whose verdict failed: its text starts with "not done:", and a run that
    # called read-only tools then gave up says "the model returned no answer". Both were
    # replayed as hits on 2026-11-02 (the Eusyra tasks failed again in 4 s).
    r"|^\s*not done:|the model returned no answer|i couldn't produce an answer"
    # The run's own verdict line ("RESULT: NOT DONE - <what is missing>"): the agent said
    # it did not do the job, so its text is not an answer to replay (2026-11-02, 20 s).
    r"|result:\s*not\s+done\b)",
    re.IGNORECASE,
)


class Status:
    HIT = "hit"
    SEED = "seed"
    MISS = "miss"

    @classmethod
    def all(cls) -> Tuple[str, str, str]:
        return (cls.HIT, cls.SEED, cls.MISS)


@dataclass
class RecallHit:
    status: str
    kind: str = ""
    record_id: str = ""
    request: str = ""
    output: str = ""
    score: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.status in (Status.HIT, Status.SEED)

    @property
    def is_hit(self) -> bool:
        return self.status == Status.HIT

    @property
    def is_seed(self) -> bool:
        return self.status == Status.SEED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "kind": self.kind,
            "record_id": self.record_id,
            "request": self.request,
            "score": round(self.score, 4),
            "provenance": dict(self.provenance),
            "output": self.output,
        }


def default_root() -> Path:
    explicit = os.environ.get("AITHER_RECALL_DIR")
    if explicit:
        return Path(explicit)
    # Containers write only to /data; the host uses ~/.aither. On Windows a
    # "/data" path is DRIVE-RELATIVE, not a container volume, so it is never a
    # data dir there -- a store under C:\data would survive no rebuild and be
    # invisible to every container.
    if os.name != "nt":
        data = Path("/data")
        if data.is_dir() and os.access(str(data), os.W_OK):
            return data / "recall"
    return Path.home() / ".aither" / "recall"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip("?!. ")


def _tokens(text: str) -> List[str]:
    return sorted({w for w in re.findall(r"[a-z0-9_]{3,}", _norm(text))})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _score(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)
    containment = inter / min(len(sa), len(sb))
    return 0.5 * jaccard + 0.5 * containment


def _record_id(kind: str, exact: str) -> str:
    return ("%s-%s" % (_norm(kind) or "any", exact[:16]))[:40]


class _Store:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_root()
        self.objects = self.root / "objects"
        self.db_path = self.root / "recall.db"
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def conn(self):
        """Commit on success, always close. ``with sqlite3.connect()`` does not
        close, which leaks handles and blocks cleanup on Windows."""
        c = self._connect()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init(self) -> None:
        with self.conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    request TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    exact TEXT NOT NULL,
                    tokens TEXT NOT NULL,
                    output_sha TEXT NOT NULL,
                    output_chars INTEGER NOT NULL DEFAULT 0,
                    request_chars INTEGER NOT NULL DEFAULT 0,
                    preview TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    verdict TEXT NOT NULL DEFAULT 'verified',
                    artifact_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL,
                    last_used REAL NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_records_exact ON records(exact, kind, scope);
                CREATE INDEX IF NOT EXISTS ix_records_created ON records(created);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT '',
                    request TEXT NOT NULL DEFAULT '',
                    record_id TEXT NOT NULL DEFAULT '',
                    score REAL NOT NULL DEFAULT 0,
                    saved_calls INTEGER NOT NULL DEFAULT 0,
                    saved_tokens INTEGER NOT NULL DEFAULT 0,
                    agent TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
                """
            )

    # -- objects -----------------------------------------------------------
    def put_object(self, text: str) -> Tuple[str, bool]:
        sha = _sha(text)
        path = self.objects / sha[:2] / sha
        created = False
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            created = True
        return sha, created

    def get_object(self, sha: str) -> str:
        try:
            return (self.objects / sha[:2] / sha).read_text(encoding="utf-8")
        except OSError:
            return ""

    # -- events ------------------------------------------------------------
    def event(self, event: str, **kw: Any) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, event, kind, scope, request, record_id, score,"
                " saved_calls, saved_tokens, agent) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(), event, kw.get("kind", ""), kw.get("scope", ""),
                    (kw.get("request", "") or "")[:300], kw.get("record_id", ""),
                    float(kw.get("score", 0.0)), int(kw.get("saved_calls", 0)),
                    int(kw.get("saved_tokens", 0)), kw.get("agent", ""),
                ),
            )


_DEFAULT: Optional[_Store] = None


def _store(root: Optional[Path] = None) -> _Store:
    global _DEFAULT
    if root is not None:
        return _Store(root)
    if _DEFAULT is None:
        _DEFAULT = _Store()
    return _DEFAULT


def _coerce_root(root: Any) -> Optional[Path]:
    return Path(root) if root else None


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------

def lookup(
    kind: str,
    request: str,
    *,
    scope: str = "",
    min_seed: float = SEED_THRESHOLD,
    hit_threshold: float = HIT_THRESHOLD,
    ttl_days: Optional[float] = DEFAULT_TTL_DAYS,
    verified_only: bool = True,
    include_output: bool = True,
    root: Any = None,
    log: bool = True,
) -> RecallHit:
    """Find a prior accepted output for ``request``.

    Never calls a model. Exact key first (O(1) index), then token similarity.
    Unverified records are invisible when ``verified_only`` (the default).
    """
    store = _store(_coerce_root(root))
    request = request or ""
    norm = _norm(request)
    exact = _sha(norm)
    tokens = _tokens(request)
    cutoff = (time.time() - float(ttl_days) * 86400) if ttl_days else 0.0

    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT * FROM records WHERE exact=? AND kind=? AND scope=?"
            " AND created>=? AND verdict='verified' ORDER BY created DESC LIMIT 1",
            (exact, kind, scope, cutoff),
        ).fetchone()
        if row is None and kind:
            # A producer-specific miss may still reuse a generic record.
            row = conn.execute(
                "SELECT * FROM records WHERE exact=? AND (kind=? OR kind='') AND scope=?"
                " AND created>=? AND verdict='verified' ORDER BY created DESC LIMIT 1",
                (exact, kind, scope, cutoff),
            ).fetchone()

        best_row = row
        best_score = 1.0 if row is not None else 0.0
        if row is None:
            sql = ("SELECT * FROM records WHERE scope=? AND created>=?"
                   + (" AND verdict='verified'" if verified_only else "")
                   + " ORDER BY created DESC LIMIT 400")
            for cand in conn.execute(sql, (scope, cutoff)):
                s = _score(tokens, json.loads(cand["tokens"] or "[]"))
                if s > best_score:
                    best_score = s
                    best_row = cand
    finally:
        conn.close()

    if best_row is not None and (best_row["exact"] == exact or best_score >= hit_threshold):
        status = Status.HIT
    elif best_row is not None and best_score >= min_seed:
        status = Status.SEED
    else:
        if log:
            store.event(Status.MISS, kind=kind, scope=scope, request=request,
                        score=best_score)
        return RecallHit(Status.MISS, kind=kind, request=request[:300], score=best_score)

    rec = dict(best_row)
    output = store.get_object(rec["output_sha"]) if include_output else ""
    # Never serve a stored failure: rows written before the failure patterns above
    # existed are still in the store, verdict 'verified'. A failure is a miss.
    if include_output and output and _CANNED_FAILURE.search(output):
        if log:
            store.event(Status.MISS, kind=kind, scope=scope, request=request,
                        score=best_score)
        return RecallHit(Status.MISS, kind=kind, request=request[:300], score=best_score)
    provenance = {
        "record_id": rec["id"],
        "agent": rec["agent"],
        "created": rec["created"],
        "age_days": round((time.time() - rec["created"]) / 86400.0, 2),
        "uses": rec["uses"],
        "source": rec["source"],
        "artifact_id": rec["artifact_id"],
        "output_sha": rec["output_sha"],
    }

    # A reused exact/near output saves one producer call; the saved input is the
    # prior output plus the prompt it would have re-paid for.
    saved_tokens = 0
    if status == Status.HIT:
        saved_tokens = (int(rec["output_chars"]) + int(rec["request_chars"])) // 4
    with store.conn() as conn:
        conn.execute(
            "UPDATE records SET uses=uses+1, last_used=? WHERE id=?",
            (time.time(), rec["id"]),
        )
    if log:
        store.event(
            status,
            kind=kind,
            scope=scope,
            request=request,
            record_id=rec["id"],
            score=best_score,
            saved_calls=1 if status == Status.HIT else 0,
            saved_tokens=saved_tokens,
        )

    return RecallHit(
        status=status,
        kind=kind,
        record_id=rec["id"],
        request=rec["request"],
        output=output,
        score=min(1.0, best_score),
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# outcome verification
# ---------------------------------------------------------------------------

def verify_outcome(
    output: Any = None,
    *,
    artifact_id: Optional[str] = None,
    artifacts: Optional[Iterable[str]] = None,
    tool_calls: Optional[Iterable[Any]] = None,
    actions: Optional[Iterable[str]] = None,
    min_chars: int = MIN_VERIFIED_CHARS,
) -> Tuple[bool, str]:
    """Did this run actually produce something?

    Returns ``(verified, reason)``. A run is verified when it produced an
    artifact, a non-read-only action/tool effect, or non-trivial output that is
    not a canned failure line. Reading directories or files is NOT production:
    a "completed" built on two ``list_directory`` calls is ``unverified``.
    A run whose text says it failed is never verified, whatever it touched.
    """
    text = output if isinstance(output, str) else (
        getattr(output, "content", None) or ("" if output is None else str(output)))
    text = (text or "").strip()
    # 2026-11-02: Eusyra rows that called crm_search/graph_query and then said "the model
    # returned no answer" were stored as verified, then replayed to every retry.
    if text and _CANNED_FAILURE.search(text):
        return False, "canned failure text presented as a result"
    if artifact_id:
        return True, "artifact:%s" % artifact_id
    if artifacts:
        real = [a for a in artifacts if a]
        if real:
            return True, "artifacts:%d" % len(real)
    if actions:
        real = [a for a in actions if (a or "").strip()]
        if real:
            return True, "actions:%d" % len(real)

    for call in tool_calls or ():
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
        if name and str(name).lower() not in _READ_ONLY_TOOLS:
            return True, "tool:%s" % name

    if len(text) < min_chars:
        return False, "no output, artifact, or side-effecting action"
    return True, "output:%d chars" % len(text)


# ---------------------------------------------------------------------------
# record (write-back)
# ---------------------------------------------------------------------------

def record(
    kind: str,
    request: str,
    output: Any,
    *,
    scope: str = "",
    agent: str = "",
    verdict: str = "verified",
    artifact_id: str = "",
    source: str = "",
    root: Any = None,
    dedupe: bool = True,
) -> Optional[Dict[str, Any]]:
    """Store one accepted output, content-addressed, for the next recall.

    A record whose ``verdict`` is not ``verified`` is kept for audit but is
    never returned by :func:`lookup` (``verified_only=True``). Content is
    written once per SHA-256; the index row links request -> agent -> verdict.
    """
    store = _store(_coerce_root(root))
    request = request or ""
    text = output if isinstance(output, str) else (
        getattr(output, "content", None) or ("" if output is None else str(output)))
    text = text or ""

    if verdict == "verified":
        ok, why = verify_outcome(text, artifact_id=artifact_id)
        if not ok:
            verdict = "unverified"
        else:
            verdict = "verified"
    norm = _norm(request)
    exact = _sha(norm)
    rid = _record_id(kind, exact)

    if verdict == "verified":
        with store.conn() as conn:
            prior = conn.execute(
                "SELECT id, output_sha FROM records WHERE exact=? AND kind=? AND scope=?"
                " AND verdict='verified' LIMIT 1",
                (exact, kind, scope),
            ).fetchone()
            if prior is not None and dedupe:
                conn.execute(
                    "UPDATE records SET last_used=?, uses=uses+1 WHERE id=?",
                    (time.time(), prior["id"]),
                )
                store.event("dedupe", kind=kind, scope=scope, request=request,
                            record_id=prior["id"], agent=agent)
                return {"id": prior["id"], "deduped": True, "verdict": "verified"}

    sha, _ = store.put_object(text)
    now = time.time()
    preview = text.strip().replace("\n", " ")[:240]
    with store.conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO records"
            " (id, kind, scope, request, norm, exact, tokens, output_sha, output_chars,"
            "  request_chars, preview, agent, verdict, artifact_id, source, created,"
            "  last_used, uses) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid, kind, scope, request[:2000], norm, exact,
                json.dumps(_tokens(request)), sha, len(text), len(request), preview,
                agent, verdict, artifact_id or "", source, now, now, 0,
            ),
        )
    store.event(
        "record", kind=kind, scope=scope, request=request, record_id=rid,
        agent=agent, saved_calls=0, saved_tokens=0,
    )
    return {"id": rid, "deduped": False, "verdict": verdict, "output_sha": sha,
            "output_chars": len(text)}


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def stats(*, days: float = 30.0, scope: str = "", root: Any = None) -> Dict[str, Any]:
    """Hit / seed / miss rates and estimated savings for the last ``days``.

    ``scope`` narrows the measurement to one tenant (the surface a spend panel
    shows); empty means every scope in this store.
    """
    store = _store(_coerce_root(root))
    cutoff = time.time() - float(days) * 86400
    ev_where = "ts>=?"
    ev_args: List[Any] = [cutoff]
    rec_where = "1=1"
    rec_args: List[Any] = []
    if scope:
        ev_where += " AND scope=?"
        ev_args.append(scope)
        rec_where += " AND scope=?"
        rec_args.append(scope)
    conn = store._connect()
    try:
        ev = {r["event"]: r for r in conn.execute(
            "SELECT event, COUNT(*) n, COALESCE(SUM(saved_calls),0) calls,"
            " COALESCE(SUM(saved_tokens),0) toks FROM events WHERE %s"
            " GROUP BY event" % ev_where, tuple(ev_args))}
        totals = conn.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN verdict='verified' THEN 1 ELSE 0 END) verified,"
            " SUM(CASE WHEN verdict='unverified' THEN 1 ELSE 0 END) unverified,"
            " COALESCE(SUM(output_chars),0) chars FROM records WHERE %s" % rec_where,
            tuple(rec_args),
        ).fetchone()
        by_kind = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM records WHERE %s"
            " GROUP BY kind ORDER BY n DESC LIMIT 10" % rec_where,
            tuple(rec_args),
        )]
    finally:
        conn.close()

    hits = ev.get(Status.HIT, {})["n"] if ev.get(Status.HIT) else 0
    seeds = ev.get(Status.SEED, {})["n"] if ev.get(Status.SEED) else 0
    misses = ev.get(Status.MISS, {})["n"] if ev.get(Status.MISS) else 0
    asked = hits + seeds + misses
    saved_calls = ev.get(Status.HIT, {})["calls"] if ev.get(Status.HIT) else 0
    saved_tokens = ev.get(Status.HIT, {})["toks"] if ev.get(Status.HIT) else 0
    return {
        "window_days": days,
        "scope": scope,
        "lookups": asked,
        "hit": hits,
        "seed": seeds,
        "miss": misses,
        "hit_rate": round(hits / asked, 4) if asked else 0.0,
        "seed_rate": round(seeds / asked, 4) if asked else 0.0,
        "saved_calls": int(saved_calls),
        "saved_tokens": int(saved_tokens),
        "records_total": int(totals["n"] or 0),
        "records_verified": int(totals["verified"] or 0),
        "records_unverified": int(totals["unverified"] or 0),
        "stored_chars": int(totals["chars"] or 0),
        "by_kind": by_kind,
        "root": str(store.root),
    }


def log_event(
    status: str,
    *,
    kind: str = "",
    request: str = "",
    scope: str = "",
    record_id: str = "",
    score: float = 0.0,
    saved_calls: int = 0,
    saved_tokens: int = 0,
    agent: str = "",
    root: Any = None,
) -> None:
    """Record a lookup outcome that a producer answered from its own store.

    The crystal store keeps a stronger validity check (file mtime/size) than
    the door's TTL; when it answers a hit it logs it here so the measurement
    ledger sees every recall, whichever layer served it.
    """
    _store(_coerce_root(root)).event(
        status, kind=kind, scope=scope, request=request, record_id=record_id,
        score=score, saved_calls=saved_calls, saved_tokens=saved_tokens, agent=agent,
    )


def recent(*, limit: int = 20, root: Any = None) -> List[Dict[str, Any]]:
    store = _store(_coerce_root(root))
    conn = store._connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, scope, request, agent, verdict, created, uses"
            " FROM records ORDER BY created DESC LIMIT ?", (int(limit),))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pruning / decay
# ---------------------------------------------------------------------------

def prune(
    *,
    ttl_days: float = 90.0,
    min_uses: int = 0,
    max_records: Optional[int] = None,
    root: Any = None,
) -> Dict[str, int]:
    """Decay stale/unused records, dedupe near-duplicates, drop dead objects.

    Every removed record is copied to ``pruned.jsonl`` first, so provenance
    survives the delete. Unreferenced objects are removed only after no record
    points at them.
    """
    store = _store(_coerce_root(root))
    cutoff = time.time() - float(ttl_days) * 86400
    removed = {"stale": 0, "duplicate": 0, "overflow": 0, "objects": 0,
               "objects_kept": 0}

    conn = store._connect()
    try:
        stale = [dict(r) for r in conn.execute(
            "SELECT * FROM records WHERE created<? AND uses<=?", (cutoff, int(min_uses))
        )]
        # Near-duplicate groups: same kind+scope, similarity >= 0.90 -> keep the
        # most used / newest, remove the rest.
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM records ORDER BY uses DESC, created DESC")]
        dead = {r["id"] for r in stale}
        seen: List[Dict[str, Any]] = []
        for r in rows:
            if r["id"] in dead:
                continue
            dup = None
            for keep in seen:
                if keep["kind"] == r["kind"] and keep["scope"] == r["scope"]:
                    if _score(json.loads(keep["tokens"] or "[]"),
                              json.loads(r["tokens"] or "[]")) >= 0.90:
                        dup = keep
                        break
            if dup is not None:
                dead.add(r["id"])
                removed["duplicate"] += 1
            else:
                seen.append(r)
        removed["stale"] = len(stale)
        if max_records is not None and len(seen) > int(max_records):
            for r in seen[int(max_records):]:
                dead.add(r["id"])
                removed["overflow"] += 1

        if dead:
            with open(store.root / "pruned.jsonl", "a", encoding="utf-8") as fh:
                for r in rows:
                    if r["id"] in dead:
                        fh.write(json.dumps({k: r[k] for k in (
                            "id", "kind", "scope", "request", "output_sha",
                            "agent", "verdict", "created")}, ensure_ascii=False) + "\n")
            conn.executemany("DELETE FROM records WHERE id=?", [(i,) for i in dead])
            conn.commit()
            live = {r["output_sha"] for r in conn.execute(
                "SELECT output_sha FROM records")}
            objs = store.objects
            for shard in objs.iterdir() if objs.exists() else []:
                if not shard.is_dir():
                    continue
                for obj in shard.iterdir():
                    if obj.name not in live:
                        try:
                            obj.unlink()
                            removed["objects"] += 1
                        except OSError:
                            # counted, never silent: a locked object is the only
                            # reason a prune leaves bytes behind
                            removed["objects_kept"] += 1
            store.event("prune", request=json.dumps(removed))
    finally:
        conn.close()
    return removed


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="recall-door-") as td:
        root = Path(td)

        # miss -> record -> hit with zero model calls
        first = lookup("forge", "add retry to the payment client", root=root)
        assert first.status == Status.MISS, first
        rec = record("forge", "add retry to the payment client",
                     "def charge(): retry(3)" * 4, agent="demiurge", root=root)
        assert rec and rec["verdict"] == "verified", rec
        again = lookup("forge", "add retry to the payment client", root=root)
        assert again.status == Status.HIT, again
        assert "retry(3)" in again.output
        assert again.score == 1.0

        # punctuation/case/whitespace are not a different question
        norm = lookup("forge", "  ADD retry to the payment CLIENT!! ", root=root)
        assert norm.status == Status.HIT, norm

        # a similar (not identical) task is a seed, not a hit
        seed = lookup("forge", "add retry logic to a payment gateway", root=root)
        assert seed.status == Status.SEED, seed

        # a genuinely different task is a miss
        other = lookup("forge", "render a title sequence for the film", root=root)
        assert other.status == Status.MISS, other

        # unverified output is never a hit
        ask = "contact the vendor about the renewal"
        bad = record("forge", ask, "", agent="demiurge", root=root)
        assert bad and bad["verdict"] == "unverified", bad
        bad_hit = lookup("forge", ask, root=root)
        assert bad_hit.status == Status.MISS, bad_hit

        # two read-only tool calls do not make a completion verified
        ok, why = verify_outcome("", tool_calls=[{"name": "list_directory"},
                                                 {"name": "list_directory"}])
        assert not ok, why
        ok2, why2 = verify_outcome("wrote the migration", tool_calls=[{"name": "write_file"}])
        assert ok2, why2
        ok3, why3 = verify_outcome("The AI backend is temporarily unavailable.")
        assert not ok3, why3
        ok4, _ = verify_outcome("", artifact_id="strata:abc")
        assert ok4

        # scope isolation: a tenant never reuses another tenant's output
        cross = lookup("forge", "add retry to the payment client", scope="other", root=root)
        assert cross.status == Status.MISS, cross

        # stats count the hit and report savings
        st = stats(root=root)
        assert st["hit"] >= 2, st
        assert st["miss"] >= 3, st
        assert st["saved_tokens"] > 0, st

        # prune keeps the used record and tombstones
        removed = prune(ttl_days=0.000001, min_uses=99, root=root)
        assert removed["stale"] >= 1, removed
        assert (root / "pruned.jsonl").exists()

    print("recall_door self-test: OK (hit, seed, miss, unverified, scope, stats, prune)")
    return 0


def _main(argv: List[str]) -> int:
    args = list(argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--self-test":
        return _self_test()
    if args[0] == "--stats":
        days = 30.0
        if "--days" in args:
            days = float(args[args.index("--days") + 1])
        print(json.dumps(stats(days=days), indent=2))
        return 0
    if args[0] == "--recent":
        n = int(args[args.index("--limit") + 1]) if "--limit" in args else 20
        print(json.dumps(recent(limit=n), indent=2))
        return 0
    if args[0] == "--prune":
        ttl = float(args[args.index("--ttl-days") + 1]) if "--ttl-days" in args else 90.0
        print(json.dumps(prune(ttl_days=ttl), indent=2))
        return 0
    if args[0] == "--lookup":
        kind = args[1] if len(args) > 1 else "any"
        request = " ".join(args[2:]) if len(args) > 2 else ""
        hit = lookup(kind, request, log=False)
        print(json.dumps(hit.to_dict(), indent=2))
        return 0 if hit.status != Status.MISS else 1
    print("unknown command: %s" % args[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

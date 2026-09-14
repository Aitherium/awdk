"""Local job engine for awdk — background job management on YOUR machine.

Design goals (owner posture: "local-only by default, opt-in portal sync"):

* **Local-first.** Jobs run on this machine via the local ``AitherAgent`` loop and
  are tracked in a small SQLite store at ``~/.aither/jobs.db``. No server, no
  network, no account required — this works fully offline.
* **True background.** ``create`` spawns a *detached* ``adk jobs _exec <id>``
  subprocess so the job outlives the launching command (the CLI is short-lived).
  ``list`` / ``status`` / ``cancel`` / ``steer`` all read & write the shared
  SQLite store, so any later ``adk`` invocation sees live state.
* **Opt-in portal sync.** ``push`` mirrors a local job to
  ``api.aitherium.com`` as a durable expedition scoped to your workspace;
  ``pull`` refreshes local rows from their remote counterparts. Sync is never
  automatic — you connect explicitly.

This mirrors the server-side expedition envelope (a job == a unit of tracked
work) but keeps a lightweight local shape so it stays fast and dependency-free.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Storage location (honours AITHER_DATA_DIR, same as adk.config) ────────────
_DATA_DIR = Path(os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
_DB_PATH = _DATA_DIR / "jobs.db"

_PORTAL_URL = os.getenv("AITHER_PORTAL_URL", "https://api.aitherium.com")

_VALID_STATUSES = ("queued", "running", "completed", "failed", "cancelled")


def _uuid() -> str:
    return str(uuid.uuid4())


# =============================================================================
# LocalJobStore — SQLite persistence (thread-local connections)
# =============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    query        TEXT NOT NULL,
    agent        TEXT DEFAULT 'aither',
    status       TEXT NOT NULL DEFAULT 'queued',
    result       TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    session_id   TEXT DEFAULT '',
    pid          INTEGER DEFAULT 0,
    remote_id    TEXT DEFAULT '',
    remote_url   TEXT DEFAULT '',
    created_at   REAL DEFAULT 0,
    updated_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Local steering: nudges applied as a follow-up turn after the current run.
CREATE TABLE IF NOT EXISTS job_steering (
    id         TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    action     TEXT NOT NULL DEFAULT 'append',
    message    TEXT NOT NULL,
    consumed   INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steering_job ON job_steering(job_id, consumed);
"""


class LocalJobStore:
    """SQLite-backed local job store at ``~/.aither/jobs.db``."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _tx(self):
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init(self) -> None:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)

    # -- Job CRUD --------------------------------------------------------------

    def create(self, query: str, *, agent: str = "aither") -> str:
        jid = _uuid()
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO jobs (id, query, agent, status, session_id, "
                "created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (jid, query, agent, f"local-{jid[:8]}", now, now),
            )
        return jid

    def update(self, job_id: str, **fields) -> None:
        allowed = {
            "status", "result", "error", "pid", "remote_id", "remote_url",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in updates)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE jobs SET {cols} WHERE id = ?",
                (*updates.values(), job_id),
            )

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE id = ? OR id LIKE ?",
            (job_id, job_id + "%"),
        ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, max_age_s: float = 7 * 24 * 3600) -> int:
        cutoff = time.time() - max_age_s
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE status IN "
                "('completed','failed','cancelled') AND updated_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    # -- Local steering --------------------------------------------------------

    def add_steering(self, job_id: str, message: str, action: str = "append") -> str:
        sid = _uuid()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO job_steering (id, job_id, action, message, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, job_id, action or "append", message, time.time()),
            )
        return sid

    def drain_steering(self, job_id: str) -> List[Dict[str, str]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT id, action, message FROM job_steering "
                "WHERE job_id = ? AND consumed = 0 ORDER BY created_at",
                (job_id,),
            ).fetchall()
            if not rows:
                return []
            conn.executemany(
                "UPDATE job_steering SET consumed = 1 WHERE id = ?",
                [(r["id"],) for r in rows],
            )
            return [{"action": r["action"], "message": r["message"]} for r in rows]


_STORE: Optional[LocalJobStore] = None


def get_store() -> LocalJobStore:
    global _STORE
    if _STORE is None:
        _STORE = LocalJobStore()
    return _STORE


# =============================================================================
# Execution — detached background subprocess + the in-process runner
# =============================================================================

def spawn(query: str, *, agent: str = "aither") -> str:
    """Create a job and launch a DETACHED ``adk jobs _exec <id>`` subprocess.

    Returns the job id immediately; the job runs in the background and writes
    its result back to the shared SQLite store.
    """
    jid = get_store().create(query, agent=agent)
    cmd = [sys.executable, "-m", "adk", "jobs", "_exec", jid]
    kwargs: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": os.getcwd(),
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive parent exit.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 — fixed argv, no shell
        get_store().update(jid, pid=proc.pid, status="running")
    except Exception as e:  # noqa: BLE001
        get_store().update(jid, status="failed", error=f"spawn failed: {e}")
    return jid


def run_foreground(query: str, *, agent: str = "aither") -> Dict[str, Any]:
    """Create + run a job synchronously in this process. Returns the final row."""
    jid = get_store().create(query, agent=agent)
    get_store().update(jid, pid=os.getpid(), status="running")
    _execute(jid)
    return get_store().get(jid) or {}


def _execute(job_id: str) -> None:
    """Run a queued/running job to completion via the local AitherAgent loop.

    This is the body of the detached ``adk jobs _exec`` subprocess. After the
    main run completes, any queued local steering is applied as follow-up
    turns on the SAME agent session (so context carries over).
    """
    import asyncio

    store = get_store()
    job = store.get(job_id)
    if not job:
        return
    if job["status"] == "cancelled":
        return

    async def _go() -> None:
        try:
            from adk.agent import AitherAgent
        except Exception as e:  # noqa: BLE001
            store.update(job_id, status="failed", error=f"adk agent unavailable: {e}")
            return
        try:
            ag = AitherAgent(job.get("agent") or "aither")
        except Exception as e:  # noqa: BLE001
            store.update(job_id, status="failed", error=f"agent init failed: {e}")
            return

        try:
            resp = await ag.run(job["query"])
            text = getattr(resp, "content", "") or ""

            # Apply any steering queued while we were running, as follow-up
            # turns on the same session (bounded so a steer flood can't loop).
            for _ in range(8):
                if (store.get(job_id) or {}).get("status") == "cancelled":
                    store.update(job_id, status="cancelled", result=text)
                    return
                nudges = store.drain_steering(job_id)
                if not nudges:
                    break
                follow = "\n".join(n["message"] for n in nudges)
                resp = await ag.run(f"[FOLLOW-UP from user]\n{follow}")
                text = (text + "\n\n" + (getattr(resp, "content", "") or "")).strip()

            store.update(job_id, status="completed", result=text)
        except Exception as e:  # noqa: BLE001
            store.update(job_id, status="failed", error=str(e)[:1000])

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        store.update(job_id, status="failed", error=str(e)[:1000])


def cancel(job_id: str) -> bool:
    """Cancel a job: mark cancelled and best-effort kill its subprocess."""
    store = get_store()
    job = store.get(job_id)
    if not job:
        return False
    if job["status"] in ("completed", "failed", "cancelled"):
        return False
    store.update(job["id"], status="cancelled")
    pid = int(job.get("pid") or 0)
    if pid and pid != os.getpid():
        try:
            if os.name == "nt":
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                os.kill(pid, 15)
        except Exception:  # noqa: BLE001
            pass
    return True


def steer(job_id: str, message: str, action: str = "append") -> bool:
    """Queue a steering nudge for a running local job (applied as a follow-up)."""
    store = get_store()
    job = store.get(job_id)
    if not job:
        return False
    store.add_steering(job["id"], message, action)
    return True


# =============================================================================
# Portal sync — opt-in mirror to portal.aitherium.com (workspace-scoped)
# =============================================================================

def _portal_headers() -> Dict[str, str]:
    """Bearer auth from the adk config (never logged). Empty if not connected."""
    try:
        from adk.config import Config
        cfg = Config.from_env()
        key = getattr(cfg, "api_key", None) or os.getenv("AITHER_API_KEY", "")
    except Exception:  # noqa: BLE001
        key = os.getenv("AITHER_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def push(job_id: str, *, portal_url: str = "") -> Dict[str, Any]:
    """Mirror a local job to the portal as a durable expedition (opt-in).

    Requires an authenticated adk (``adk login`` / ``adk connect``) — the job is
    created under your workspace via the portal's expedition API. Records the
    remote id locally so ``pull`` can refresh it. Returns {ok, remote_id|error}.
    """
    import requests

    store = get_store()
    job = store.get(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    base = (portal_url or _PORTAL_URL).rstrip("/")
    headers = _portal_headers()
    if "Authorization" not in headers:
        return {"ok": False, "error": "not connected — run `adk login` first"}
    try:
        # The portal exposes the same expedition envelope; a simple job is a
        # single-task expedition. Use the intake create with auto-approve.
        resp = requests.post(
            f"{base}/expedition/intake",
            json={"goal": job["query"], "title": job["query"][:80],
                  "auto_approve": True, "source": "adk-local"},
            headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            return {"ok": False, "error": f"portal {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        remote_id = data.get("id") or data.get("expedition_id") or ""
        store.update(job["id"], remote_id=remote_id, remote_url=base)
        return {"ok": True, "remote_id": remote_id}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def pull(job_id: str) -> Dict[str, Any]:
    """Refresh a synced job's status/result from its portal counterpart."""
    import requests

    store = get_store()
    job = store.get(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    remote_id = job.get("remote_id")
    base = (job.get("remote_url") or _PORTAL_URL).rstrip("/")
    if not remote_id:
        return {"ok": False, "error": "job not synced (push it first)"}
    headers = _portal_headers()
    try:
        st = requests.get(
            f"{base}/expedition/{remote_id}/status", headers=headers, timeout=30,
        )
        if st.status_code >= 400:
            return {"ok": False, "error": f"portal {st.status_code}"}
        summary = st.json()
        remote_status = str(summary.get("status", "")).lower()
        mapped = {
            "completed": "completed", "active": "running", "planning": "running",
            "failed": "failed", "blocked": "running",
        }.get(remote_status, job["status"])
        fields: Dict[str, Any] = {"status": mapped}
        # Best-effort: pull the task result_summary as the answer.
        try:
            tk = requests.get(
                f"{base}/expedition/{remote_id}/tasks", headers=headers, timeout=30,
            )
            if tk.ok:
                tasks = tk.json().get("tasks", tk.json()) if tk.text else []
                if isinstance(tasks, list) and tasks:
                    fields["result"] = str(tasks[0].get("result_summary", "") or "")
        except Exception:  # noqa: BLE001
            pass
        store.update(job["id"], **fields)
        return {"ok": True, "status": mapped}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

"""Plan persistence. One JSON file per plan; reads are owner-scoped."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("adk.create_flow")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
#: One lock for the process: engines are built per request, the files are shared.
_LOCK = threading.Lock()


class PlanStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = _LOCK

    @staticmethod
    def bucket(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:8]

    def new_id(self, owner: str) -> str:
        """<owner bucket><random>: list() globs the bucket instead of reading every plan."""
        return f"{self.bucket(owner)}{uuid.uuid4().hex[:16]}"

    def _path(self, plan_id: str) -> Path:
        if not _ID_RE.match(plan_id or ""):
            raise KeyError(plan_id)
        return self.root / f"{plan_id}.json"

    def save(self, plan: dict[str, Any]) -> None:
        path = self._path(plan["id"])
        data = json.dumps(plan, indent=2, default=str)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(data)
                self._replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    @staticmethod
    def _replace(tmp: str, path: Path) -> None:
        # Windows refuses to replace a file another thread has open for reading; the
        # reader holds it for microseconds, so a short retry is the honest fix.
        for attempt in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.01)

    def load(self, plan_id: str) -> Optional[dict[str, Any]]:
        try:
            path = self._path(plan_id)
        except KeyError:
            return None
        for attempt in range(20):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                # A writer is mid-replace (Windows); the window is microseconds.
                time.sleep(0.01)
        return None

    def claim(self, plan_id: str, expect: str, new: str) -> Optional[dict[str, Any]]:
        """Compare-and-swap a plan's status across processes (Genesis runs two instances).

        An O_EXCL lock file serialises the read-check-write; None means the plan is gone or
        was not in ``expect``, so exactly ONE of two concurrent approves wins.
        """
        lock = self._path(plan_id).with_suffix(".lock")
        deadline = time.time() + 5
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:  # a lock older than 30 s belongs to a dead process
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink()
                        continue
                except OSError as exc:
                    logger.debug("stale-lock check failed: %s", exc)
                if time.time() > deadline:
                    return None
                time.sleep(0.02)
        try:
            plan = self.load(plan_id)
            if not plan or plan.get("status") != expect:
                return None
            plan["status"] = new
            plan["updated_at"] = time.time()
            self.save(plan)
            return plan
        finally:
            try:
                lock.unlink()
            except OSError as exc:
                logger.debug("lock already gone: %s", exc)

    def prune(self, owner: str, keep: int = 50) -> None:
        """Bound what one user can pile up: drop the oldest finished plans past ``keep``."""
        for old in self.list(owner, limit=10_000)[keep:]:
            if old.get("status") in ("deployed", "failed", "undone", "blocked", "planned"):
                try:
                    self._path(old["id"]).unlink()
                except OSError as exc:
                    logger.debug("prune skipped %s: %s", old.get("id"), exc)

    def list(self, owner: str, limit: int = 30) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.root.glob(f"{self.bucket(owner)}*.json"):
            try:
                plan = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if plan.get("owner") == owner:
                out.append(plan)
        out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return out[:limit]

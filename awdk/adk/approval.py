"""Human-in-the-loop tool approval — pause before a gated tool, resume on decision.

The managed control plane (AitherOS) drives this self-hosted agent and shows the customer
an Allow/Deny card when the agent wants to run a sensitive tool. To support that — and to
survive a customer approving *days* later or after a restart — the pause state is persisted
to disk keyed by ``session_id``; resume is a fresh ``/sessions/{id}/confirm`` request, not a
held connection.

Policy: a tool needs approval if it is in the agent's ``always_ask`` set. That set is read
from the ``AITHER_TOOL_APPROVAL`` env var — a comma-separated list of tool names, or ``*``
for every tool — (optionally namespaced ``agent:tool``). Empty = no gating (back-compat).

Decisions are keyed by ``(session_id, tool_name)`` rather than the provider's per-call
``tool_use_id`` because resuming re-runs the turn (adk rebuilds the loop from memory each
call), which mints fresh ids. The stored ``pending`` list maps the original ids → names so a
client that approves by ``tool_use_id`` still resolves to the right tool.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "ApprovalStore", "get_approval_store", "needs_approval", "decision_for",
]


def _state_dir() -> Path:
    env = os.getenv("AITHER_ADK_STATE_DIR", "").strip()
    base = Path(env) if env else (Path.home() / ".aither" / "adk")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _policy_set() -> set[str]:
    """The configured always-ask tool names (lowercased). ``*`` = all tools."""
    raw = os.getenv("AITHER_TOOL_APPROVAL", "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def needs_approval(agent_name: str, tool_name: str) -> bool:
    """True if ``tool_name`` is gated for ``agent_name`` per the approval policy."""
    pol = _policy_set()
    if not pol:
        return False
    if "*" in pol:
        return True
    t = (tool_name or "").lower()
    a = (agent_name or "").lower()
    return t in pol or f"{a}:{t}" in pol


class ApprovalStore:
    """Disk-backed per-session pause + decision store (JSON, lock-guarded)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else (_state_dir() / "paused_turns.json")
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(session_id)

    def put_pending(self, session_id: str, *, user_message: str, agent: str,
                    pending: list[dict]) -> None:
        """Persist a paused turn (or refresh its pending list), preserving any decisions
        already recorded for this session."""
        with self._lock:
            data = self._load()
            entry = data.get(session_id) or {}
            entry.update({
                "user_message": user_message, "agent": agent,
                "pending": pending, "updated_at": time.time(),
            })
            entry.setdefault("decisions", {})
            data[session_id] = entry
            self._save(data)

    def record_decisions(self, session_id: str, decisions: list[dict]) -> dict[str, str]:
        """Record allow/deny decisions. Each decision may carry ``tool`` directly or a
        ``tool_use_id`` resolved against the stored pending list. Returns the merged
        ``{tool_name: result}`` map."""
        with self._lock:
            data = self._load()
            entry = data.get(session_id) or {"decisions": {}}
            id_to_tool = {p.get("tool_use_id"): p.get("tool") for p in entry.get("pending", [])}
            merged: dict[str, str] = dict(entry.get("decisions") or {})
            for d in decisions or []:
                result = str((d or {}).get("result") or "allow").lower()
                result = result if result in ("allow", "deny") else "allow"
                tool = (d or {}).get("tool") or id_to_tool.get((d or {}).get("tool_use_id"))
                if tool:
                    merged[str(tool).lower()] = result
            entry["decisions"] = merged
            entry["updated_at"] = time.time()
            data[session_id] = entry
            self._save(data)
            return merged

    def decision_for(self, session_id: str, tool_name: str) -> str | None:
        """A recorded 'allow'/'deny' for this session+tool, or None if undecided."""
        entry = self.get(session_id)
        if not entry:
            return None
        return (entry.get("decisions") or {}).get((tool_name or "").lower())

    def clear(self, session_id: str) -> None:
        with self._lock:
            data = self._load()
            if session_id in data:
                data.pop(session_id, None)
                self._save(data)


_STORE: ApprovalStore | None = None
_STORE_LOCK = threading.Lock()


def get_approval_store() -> ApprovalStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ApprovalStore()
        return _STORE


def decision_for(session_id: str, tool_name: str) -> str | None:
    return get_approval_store().decision_for(session_id, tool_name)

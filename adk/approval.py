"""Human-in-the-loop tool approval — pause before a gated tool, resume on decision.

The managed control plane (AitherOS) drives this self-hosted agent and shows the customer
an Allow/Deny card when the agent wants to run a sensitive tool. To support that — and to
survive a customer approving *days* later or after a restart — the pause state is persisted
to disk keyed by ``session_id``; resume is a fresh ``/sessions/{id}/confirm`` request, not a
held connection.

Policy: a tool needs approval if it is in the agent's ``always_ask`` set. That set is read
from the ``AITHER_TOOL_APPROVAL`` env var — a comma-separated list of tool names, or ``*``
for every tool — (optionally namespaced ``agent:tool``). Empty = no gating (back-compat).

Decisions are keyed PER CALL: ``(session_id, tool_name, sha256(canonical args))``. Not the
provider's ``tool_use_id`` -- resuming re-runs the turn (adk rebuilds the loop from memory each
call), which mints fresh ids -- but not the bare tool name either: an "allow" for
``send_email(to=a)`` must not also allow ``send_email(to=b)`` later in the same resumed turn.
The stored ``pending`` list maps the original ids -> (tool, args), so a client that approves by
``tool_use_id`` OR by tool name resolves to exactly the calls on the card.

Compatibility: ``decision_for(session_id, tool_name)`` without ``args`` still answers the
tool-level decision (the label a host prints). With ``args`` it answers for that call: the
recorded per-call decision; else a tool-level ``deny`` (deny is never narrowed); else, when
per-call decisions exist for the tool, ``None`` (these args were not on the card -- ask
again); else the tool-level decision (a decision recorded with no pending args to key on).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "ApprovalStore", "get_approval_store", "needs_approval", "decision_for",
    "set_runtime_gates", "runtime_gates", "call_key",
]


def call_key(tool_name: str, args: Any) -> str:
    """``tool#sha256(canonical args)`` -- the key one call's decision is stored under.

    Canonical form matches :func:`adk.receipts.digest` for a dict (sorted keys, compact
    separators), so a receipt's ``args_sha256`` names the same call. A non-dict is keyed
    as ``{}``, exactly as the agent loop stores a pending call's args.
    """
    payload = args if isinstance(args, dict) else {}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return f"{(tool_name or '').lower()}#{hashlib.sha256(data).hexdigest()}"


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


#: Runtime gates added by a host for one agent (lowercased agent -> tool names), on
#: top of ``AITHER_TOOL_APPROVAL``. The Hearth taint guard uses it: once a session
#: has read private or web text, ``web_fetch`` / ``web_search`` ask first.
_RUNTIME_GATES: dict[str, set[str]] = {}
_GATES_LOCK = threading.Lock()


def set_runtime_gates(agent_name: str, tools: Any) -> None:
    """Gate exactly ``tools`` for ``agent_name`` at runtime (empty = none)."""
    names = {str(t).lower() for t in (tools or ()) if t}
    with _GATES_LOCK:
        if names:
            _RUNTIME_GATES[(agent_name or "").lower()] = names
        else:
            _RUNTIME_GATES.pop((agent_name or "").lower(), None)


def runtime_gates(agent_name: str) -> set[str]:
    with _GATES_LOCK:
        return set(_RUNTIME_GATES.get((agent_name or "").lower(), ()))


def needs_approval(agent_name: str, tool_name: str) -> bool:
    """True if ``tool_name`` is gated for ``agent_name`` per the approval policy
    (``AITHER_TOOL_APPROVAL``) or a runtime gate (:func:`set_runtime_gates`)."""
    if (tool_name or "").lower() in runtime_gates(agent_name):
        return True
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
        """Owner-only (0600 / icacls), atomically. A store another local user could
        rewrite would let them record an ``allow``, so a failure to restrict raises
        :class:`adk._private_file.PrivateFileError` and nothing is written."""
        from adk._private_file import write_private_text

        write_private_text(self.path, json.dumps(data, indent=2))

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
        ``tool_use_id`` resolved against the stored pending list. Fail-closed: anything
        but an explicit ``result: "allow"`` (missing, malformed, unknown) records
        ``"deny"``. Returns the merged ``{tool_name: result}`` map (unchanged shape).

        Per call: a ``tool_use_id`` decision applies to THAT pending call's args; a
        ``tool`` decision applies to every pending call of that tool (each keyed by its
        own args). Those per-call results land in ``call_decisions`` so a later call of
        the same tool with different args is not covered by this answer."""
        with self._lock:
            data = self._load()
            entry = data.get(session_id) or {"decisions": {}}
            pending = [p for p in (entry.get("pending") or []) if isinstance(p, dict)]
            by_id = {p.get("tool_use_id"): p for p in pending if p.get("tool_use_id")}
            merged: dict[str, str] = dict(entry.get("decisions") or {})
            calls: dict[str, str] = dict(entry.get("call_decisions") or {})
            for d in decisions or []:
                d = d or {}
                # Fail closed: only an explicit "allow" grants the tool. A missing,
                # empty, non-string or unrecognised result is recorded as "deny".
                raw = d.get("result")
                result = raw.strip().lower() if isinstance(raw, str) else ""
                result = "allow" if result == "allow" else "deny"
                hit = by_id.get(d.get("tool_use_id"))
                tool = d.get("tool") or (hit or {}).get("tool")
                if not tool:
                    continue
                tool = str(tool).lower()
                merged[tool] = result
                if hit is not None:
                    targets = [hit]
                else:
                    targets = [p for p in pending if str(p.get("tool") or "").lower() == tool]
                for p in targets:
                    calls[call_key(tool, p.get("args"))] = result
            entry["decisions"] = merged
            entry["call_decisions"] = calls
            entry["updated_at"] = time.time()
            data[session_id] = entry
            self._save(data)
            return merged

    def decision_for(self, session_id: str, tool_name: str,
                     args: Any = None) -> str | None:
        """A recorded 'allow'/'deny' for this session+tool (+call), or None if undecided.

        Without ``args``: the tool-level decision (back-compat). With ``args``: see the
        module docstring -- an allow never stretches to arguments that were not on the card.
        """
        entry = self.get(session_id)
        if not entry:
            return None
        tool = (tool_name or "").lower()
        tool_level = (entry.get("decisions") or {}).get(tool)
        if args is None:
            return tool_level
        calls = entry.get("call_decisions") or {}
        exact = calls.get(call_key(tool, args))
        if exact is not None:
            return exact
        if tool_level == "deny":
            return "deny"
        if any(k.startswith(f"{tool}#") for k in calls):
            return None
        return tool_level

    def take_decided(self, session_id: str) -> tuple[list[dict], list[dict]]:
        """Claim the pending calls that now carry a decision: ``(allowed, denied)``.

        Each call is handed out ONCE -- its key is recorded under ``executed`` in the
        same locked write -- so a resume that is retried, or two resumes racing, can
        never run an approved call twice. Undecided calls stay pending.
        """
        with self._lock:
            data = self._load()
            entry = data.get(session_id)
            if not entry:
                return [], []
            calls = entry.get("call_decisions") or {}
            done = set(entry.get("executed") or [])
            allowed: list[dict] = []
            denied: list[dict] = []
            for p in entry.get("pending") or []:
                if not isinstance(p, dict):
                    continue
                key = call_key(str(p.get("tool") or ""), p.get("args"))
                verdict = calls.get(key)
                if key in done or verdict not in ("allow", "deny"):
                    continue
                (allowed if verdict == "allow" else denied).append(p)
                done.add(key)
            entry["executed"] = sorted(done)
            data[session_id] = entry
            self._save(data)
            return allowed, denied

    def clear(self, session_id: str) -> None:
        """Drop the paused turn and every recorded decision for this session."""
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


def decision_for(session_id: str, tool_name: str, args: Any = None) -> str | None:
    return get_approval_store().decision_for(session_id, tool_name, args)

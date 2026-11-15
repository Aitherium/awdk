"""Tool-call capture -- one JSONL row per tool call, the raw material for training.

Every call that passes through :meth:`adk.tools.ToolRegistry.execute` appends one
row to ``~/.aither/tool_calls/YYYY-MM-DD.jsonl`` (UTC day). A downstream harvester
turns those rows into supervised tool-use examples and world-model transitions.

Row shape (stable; harvesters key on it)::

    {"ts": "2026-11-09T12:00:00.123+00:00", "session_id": "...", "agent": "...",
     "tool": "read_file", "args": {...}, "result": "...", "ok": true,
     "error_kind": "", "latency_ms": 12.3, "surface": "awdk"}

Guarantees:

* **Redacted.** Secret-looking keys (password/token/key/secret/...) are replaced
  wholesale; secret-shaped values (``sk-``, ``ghp_``, ``aither_sk_``, ``Bearer``,
  PEM blocks, JWTs, AWS keys ...) are masked inside any string, in args and result.
* **Bounded.** ``result`` is cut to :data:`RESULT_MAX_CHARS`; every string inside
  ``args`` is cut to :data:`ARG_STR_MAX_CHARS`.
* **Off switches.** ``AITHER_TOOL_CAPTURE=0`` disables capture. Private mode
  (``AITHER_PRIVATE=1``, ``AITHER_TELEMETRY_PRIVACY=private``, or
  :func:`private_mode`) writes nothing at all.
* **Never raises.** A capture failure is logged once at debug and swallowed; the
  tool call it describes is unaffected.

``AITHER_TOOL_CALLS_DIR`` overrides the output directory.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("adk.tool_capture")

ENV_SWITCH = "AITHER_TOOL_CAPTURE"
ENV_DIR = "AITHER_TOOL_CALLS_DIR"
ENV_PRIVATE = "AITHER_PRIVATE"
ENV_TELEMETRY_PRIVACY = "AITHER_TELEMETRY_PRIVACY"

RESULT_MAX_CHARS = 2000
ARG_STR_MAX_CHARS = 2000
_MAX_DEPTH = 6
_MAX_ITEMS = 200
REDACTED = "[REDACTED]"

_FALSY = ("0", "false", "no", "off")
_TRUTHY = ("1", "true", "yes", "on")

#: Per-task private flag (a turn can be private without flipping the process env).
_PRIVATE_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "adk_tool_capture_private", default=False
)

#: A key whose VALUE is a credential. Matched against the lower-cased key with
#: ``-`` folded to ``_``. Counts (``max_tokens``, ``token_count``) are not secrets.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|pwd|passphrase|secret|secrets|token|api_?key|apikey|"
    r"access_?key|private_?key|secret_?key|client_?secret|credential|credentials|"
    r"authorization|auth|cookie|bearer|key|session_?key|signature)$"
)
_NOT_SECRET_KEY_RE = re.compile(r"(?:tokens|_count|_len|_limit|_budget|_max|_min)$")

#: Secret-shaped substrings inside any string.
_SECRET_VALUE_RES = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S
    ),
    re.compile(r"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\baither_(?:sk|ext|pk)_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{12,}"),
    # key=value / "key": "value" pairs inside free text (shell commands, URLs, logs)
    re.compile(
        r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret)\s*[=:]\s*[\"']?)([^\s\"'&,;}]{4,})"
    ),
)

_lock = threading.Lock()
_warned = False


# ── switches ──────────────────────────────────────────────────────────────────


def is_private() -> bool:
    """Whether the current context is private (nothing may be captured)."""
    if _PRIVATE_CTX.get():
        return True
    if os.environ.get(ENV_PRIVATE, "").strip().lower() in _TRUTHY:
        return True
    return os.environ.get(ENV_TELEMETRY_PRIVACY, "").strip().lower() == "private"


def enabled() -> bool:
    """Capture is on unless switched off or private."""
    if os.environ.get(ENV_SWITCH, "1").strip().lower() in _FALSY:
        return False
    return not is_private()


@contextlib.contextmanager
def private_mode(on: bool = True) -> Iterator[None]:
    """Mark the enclosed tool calls private (task-local; nothing is captured)."""
    token = _PRIVATE_CTX.set(bool(on))
    try:
        yield
    finally:
        _PRIVATE_CTX.reset(token)


def capture_dir() -> Path:
    """Directory rows are appended under (``AITHER_TOOL_CALLS_DIR`` overrides)."""
    override = os.environ.get(ENV_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aither" / "tool_calls"


# ── redaction ─────────────────────────────────────────────────────────────────


def _is_secret_key(key: Any) -> bool:
    k = str(key).strip().lower().replace("-", "_")
    if not k or _NOT_SECRET_KEY_RE.search(k):
        return False
    return bool(_SECRET_KEY_RE.search(k))


def redact_text(text: str) -> str:
    """Mask secret-shaped substrings in ``text``."""
    if not text:
        return text
    for rx in _SECRET_VALUE_RES:
        if rx.groups == 2:
            text = rx.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = rx.sub(REDACTED, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a JSON-safe, redacted, size-bounded copy of ``value``."""
    if _depth > _MAX_DEPTH:
        return "[...]"
    if isinstance(value, str):
        return redact_text(value[: ARG_STR_MAX_CHARS + 256])[:ARG_STR_MAX_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["..."] = f"{len(value) - _MAX_ITEMS} more"
                break
            if _is_secret_key(k) and v not in (None, "", [], {}):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out_list = [redact(v, _depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out_list.append(f"... {len(items) - _MAX_ITEMS} more")
        return out_list
    return redact(repr(value), _depth + 1)


def redact_result(text: Any) -> str:
    """Redact and cut a tool result to :data:`RESULT_MAX_CHARS`."""
    s = text if isinstance(text, str) else str(text)
    return redact_text(s[: RESULT_MAX_CHARS + 256])[:RESULT_MAX_CHARS]


# ── row + writer ──────────────────────────────────────────────────────────────


def current_session_id() -> str:
    """The session of the turn executing now, when the agent loop set one."""
    mod = sys.modules.get("adk.agent")
    ctx = getattr(mod, "TOOL_SESSION_CTX", None) if mod is not None else None
    if ctx is None:
        return ""
    try:
        return str(ctx.get() or "")
    except Exception:  # noqa: BLE001 -- a context lookup must never break a tool call
        return ""


def build_row(
    *,
    tool: str,
    args: Any,
    result: Any,
    ok: bool,
    error_kind: str = "",
    latency_ms: float = 0.0,
    agent: str = "",
    session_id: str = "",
    surface: str = "awdk",
) -> dict[str, Any]:
    """The redacted row for one call."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": session_id or "",
        "agent": agent or "",
        "tool": tool,
        "args": redact(args if args is not None else {}),
        "result": redact_result(result if result is not None else ""),
        "ok": bool(ok),
        "error_kind": error_kind or "",
        "latency_ms": round(float(latency_ms), 1),
        "surface": surface,
    }


def _append(row: dict[str, Any]) -> None:
    d = capture_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{row['ts'][:10]}.jsonl"
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with _lock:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def record_tool_call(
    *,
    tool: str,
    args: Any,
    result: Any,
    ok: bool,
    error_kind: str = "",
    latency_ms: float = 0.0,
    agent: str = "",
    session_id: str | None = None,
    surface: str = "awdk",
) -> bool:
    """Append one row. Returns whether a row was written; never raises."""
    global _warned
    try:
        if not enabled():
            return False
        sid = current_session_id() if session_id is None else session_id
        agent_name = agent or os.environ.get("AITHER_AGENT_NAME", "")
        _append(
            build_row(
                tool=tool,
                args=args,
                result=result,
                ok=ok,
                error_kind=error_kind,
                latency_ms=latency_ms,
                agent=agent_name,
                session_id=sid,
                surface=surface,
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- capture must never reach the tool path
        if not _warned:
            _warned = True
            logger.debug("tool capture disabled for this call: %s", type(exc).__name__)
        return False


def record_outcome(
    session_id: str,
    *,
    ok: bool,
    reward: float | None = None,
    note: str = "",
    surface: str = "awdk",
) -> bool:
    """Append a turn/session OUTCOME row the harvester joins to that session's calls.

    ``kind="outcome"`` rows carry no tool; a harvester labels every call of the
    session with ``ok``/``reward``. Same switches as :func:`record_tool_call`.
    """
    try:
        if not enabled() or not session_id:
            return False
        _append(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "kind": "outcome",
                "session_id": session_id,
                "ok": bool(ok),
                "reward": None if reward is None else float(reward),
                "note": redact_text(str(note))[:500],
                "surface": surface,
            }
        )
        return True
    except Exception:  # noqa: BLE001 -- never raise into the caller
        return False


__all__ = [
    "ENV_DIR",
    "ENV_PRIVATE",
    "ENV_SWITCH",
    "RESULT_MAX_CHARS",
    "build_row",
    "capture_dir",
    "current_session_id",
    "enabled",
    "is_private",
    "private_mode",
    "record_outcome",
    "record_tool_call",
    "redact",
    "redact_result",
    "redact_text",
]

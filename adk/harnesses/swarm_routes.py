"""``/swarms/*`` on the AitherShell harness daemon (:8362): SwarmBoard bridge + local harness worker delegation.

Bridges ``awsh`` CLI, ``awsh`` MCP tools, and paired ``awconnect`` browser companions
to Genesis's live ``SwarmBoardEngine`` (``/swarms/*``) while exposing this machine's
local ``awsh`` harness capacity (``claude``, ``codex``, ``gemini``, ``aither``, ``awdk``)
and letting any SwarmBoard worker slot delegate directly to a local harness session
whose ``[done]`` summary and artifacts stream back onto the Swarm Board.

Security (fail-closed):
- Genesis writes (create/steer/pause/control/post) REQUIRE an internal key
  (``AITHER_INTERNAL_SECRET`` / ``AITHER_INTERNAL_KEY`` / ``~/.aither/internal_key``)
  or operator session bearer (``AITHER_SESSION_BEARER`` / ``~/.aither/session-bearer``);
  missing credentials raise :class:`SwarmAuthError` (HTTP 401) and secrets are never
  printed or logged.
- Launch, pause, control, and harness delegation on ``:8362`` are strictly owner-only
  (``require_owner``); paired ``awconnect`` tokens may read ``GET /swarms*`` and post
  attributed human steers (``POST /swarms/{id}/steer``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("adk.harnesses.swarm_routes")

__all__ = [
    "SWARM_HARNESSES",
    "SwarmApiError",
    "SwarmAuthError",
    "SwarmGenesisClient",
    "delegate_harness_worker",
    "extract_session_artifacts",
    "extract_session_summary",
    "local_swarm_capacity",
    "mount",
    "swarm_ui_url",
]

#: Core coding/agent harnesses a SwarmBoard worker slot can delegate to on this machine.
SWARM_HARNESSES: tuple[str, ...] = ("claude", "codex", "gemini", "aither", "awdk")

_SWARM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
_ARTIFACT_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:Screenshot|Artifact)\s*:\s*([^\s|\n]+)(?:\s*\|\s*([^\n]+))?",
    re.I,
)
_CA_CANDIDATES = (
    "/usr/local/share/aither/ca-chain.pem",
    "/etc/aither/ca-chain.pem",
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "aitheros-genesis"}


class SwarmApiError(RuntimeError):
    """Error talking to Genesis ``/swarms/*`` (carries HTTP status)."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class SwarmAuthError(SwarmApiError):
    """Fail-closed auth refusal when no Genesis credential is configured."""

    def __init__(self, message: str = "Swarm write refused: missing AITHER_INTERNAL_SECRET or session bearer") -> None:
        super().__init__(message, status=401)


def _genesis_base() -> str:
    """Resolve the host-reachable Genesis Swarm API base URL.

    Resolution order:
    1. ``AITHER_SWARM_API_URL`` (explicit Swarm API override, e.g. an in-network
       ``https://aitheros-genesis:8001`` or a custom portal/gateway bridge).
    2. ``adk.control_plane.genesis_override()`` (``AITHER_GENESIS_URL`` /
       ``AITHER_API_URL``, skipping MCP gateway hosts that do not serve Genesis).
    3. ``adk.control_plane.genesis_data_base()`` (``<control plane>/api/bridge/genesis``,
       default ``https://api.aitherium.com/api/bridge/genesis``) — NEVER
       ``https://127.0.0.1:8001``, since Genesis publishes no host port.
    """
    explicit = (os.environ.get("AITHER_SWARM_API_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from adk.control_plane import genesis_data_base  # noqa: PLC0415

        return genesis_data_base().rstrip("/")
    except Exception:  # noqa: BLE001
        fallback = (
            os.environ.get("AITHER_GENESIS_URL")
            or os.environ.get("AITHER_API_URL")
            or "https://api.aitherium.com/api/bridge/genesis"
        ).strip()
        return fallback.rstrip("/")


def swarm_ui_url(swarm: Optional[Dict[str, Any]] = None, swarm_id: str = "") -> str:
    """Canonical 4-tab Live Swarm UI URL (explicit override -> swarm payload -> default)."""
    base = (os.environ.get("AITHER_SWARM_UI_URL") or "https://app.aitherium.com/swarm").strip().rstrip("/")
    if isinstance(swarm, dict):
        raw = str(swarm.get("ui_url") or "").strip()
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith("/"):
            return urllib.parse.urljoin(base + "/", raw)
    sid = str(swarm_id or (swarm.get("swarm_id") if isinstance(swarm, dict) else "") or "").strip()
    return f"{base}?id={urllib.parse.quote(sid)}" if sid else base


def _internal_key() -> str:
    for var in ("AITHER_INTERNAL_SECRET", "AITHER_INTERNAL_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        return (Path.home() / ".aither" / "internal_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _session_bearer() -> str:
    for var in ("AITHER_SESSION_BEARER", "AITHER_API_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        tok = (Path.home() / ".aither" / "session-bearer").read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    try:
        from adk.config import load_saved_config  # noqa: PLC0415

        cfg = load_saved_config()
        tok = str(cfg.get("access_token") or cfg.get("api_key") or "").strip()
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        pass
    return ""


def _ssl_context_for(url: str) -> Optional[ssl.SSLContext]:
    if not url.lower().startswith("https://"):
        return None
    ca = (os.environ.get("AITHER_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or "").strip()
    if ca and Path(ca).is_file():
        return ssl.create_default_context(cafile=ca)
    for cand in _CA_CANDIDATES:
        if Path(cand).is_file():
            return ssl.create_default_context(cafile=cand)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _validate_swarm_id(swarm_id: str) -> str:
    sid = str(swarm_id or "").strip()
    if not _SWARM_ID_RE.match(sid) or ".." in sid:
        raise SwarmApiError(f"invalid swarm_id {sid!r}", status=422)
    return sid


class SwarmGenesisClient:
    """Thin fail-closed HTTP client for Genesis ``/swarms/*``."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        internal_key: Optional[str] = None,
        bearer: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = (base_url if base_url is not None else _genesis_base()).rstrip("/")
        self._explicit_key = internal_key
        self._explicit_bearer = bearer
        self._opener = opener
        self.timeout_s = timeout_s

    def _auth_headers(self, *, require_auth: bool) -> Dict[str, str]:
        key = _internal_key() if self._explicit_key is None else self._explicit_key
        bearer = _session_bearer() if self._explicit_bearer is None else self._explicit_bearer
        headers: Dict[str, str] = {"Accept": "application/json"}
        if key:
            headers["X-Internal-Key"] = key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if require_auth and not (key or bearer):
            raise SwarmAuthError()
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        require_auth: bool = False,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(require_auth=require_auth)
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        opener = self._opener or urllib.request.urlopen
        try:
            if self._opener is not None:
                resp = opener(req, timeout=self.timeout_s)
            else:
                resp = opener(req, timeout=self.timeout_s, context=_ssl_context_for(url))
            with resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(err_raw) if err_raw else {}
                detail = str(parsed.get("detail") or parsed.get("error") or err_raw[:240])
            except Exception:  # noqa: BLE001
                detail = str(exc.reason or exc.code)
            raise SwarmApiError(detail or f"Genesis HTTP {exc.code}", status=int(exc.code)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SwarmApiError(f"Genesis unreachable at {self.base_url}: {exc}", status=503) from exc
        if not raw.strip():
            return {}
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SwarmApiError(f"Genesis returned non-JSON for {path}", status=502) from exc
        return out if isinstance(out, dict) else {"data": out}

    def list_swarms(
        self,
        *,
        status: Optional[str] = None,
        guild: Optional[str] = None,
        channel: Optional[str] = None,
        thread: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            params["status"] = status
        if guild:
            params["guild"] = guild
        if channel:
            params["channel"] = channel
        if thread:
            params["thread"] = thread
        qs = urllib.parse.urlencode(params)
        return self.request("GET", f"/swarms?{qs}", require_auth=False)

    def get_swarm(self, swarm_id: str) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("GET", f"/swarms/{urllib.parse.quote(sid)}", require_auth=False)

    def create_swarm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/swarms", payload, require_auth=True)

    def steer_swarm(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/steer", payload, require_auth=True)

    def pause_swarm(self, swarm_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/pause", payload or {}, require_auth=True)

    def control_swarm(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/control", payload, require_auth=True)

    def post_message(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/post", payload, require_auth=True)

    def probe_reachable(self, timeout_s: float = 3.0) -> Dict[str, Any]:
        prev_timeout = self.timeout_s
        self.timeout_s = min(prev_timeout, max(0.5, float(timeout_s)))
        try:
            data = self.list_swarms(limit=1)
            return {
                "reachable": True,
                "url": f"{self.base_url}/swarms",
                "count": int(data.get("count") or len(data.get("swarms") or [])),
            }
        except SwarmApiError as exc:
            return {
                "reachable": False,
                "url": f"{self.base_url}/swarms",
                "status": exc.status,
                "error": str(exc),
                "hint": "Set AITHER_SWARM_API_URL to a reachable Genesis or control-plane bridge URL.",
            }
        finally:
            self.timeout_s = prev_timeout


def _detect_installed_harnesses() -> List[str]:
    try:
        from adk.harnesses.registry import SPECS, detect

        out: List[str] = []
        for hid in SPECS:
            info = detect(hid)
            if info.get("available"):
                out.append(hid)
        return sorted(out)
    except Exception:  # noqa: BLE001
        return []


def local_swarm_capacity(
    mgr: Any = None,
    *,
    genesis: Optional[SwarmGenesisClient] = None,
    installed_ids: Optional[Sequence[str]] = None,
    probe_genesis: bool = False,
) -> Dict[str, Any]:
    """Advertise this machine's local Swarm + ``awsh`` harness execution capacity."""
    from adk.harnesses.manager import MAX_SESSIONS

    installed = list(installed_ids) if installed_ids is not None else _detect_installed_harnesses()
    swarm_harnesses = [h for h in SWARM_HARNESSES if h in installed]
    sessions = mgr.list_sessions() if mgr is not None and callable(getattr(mgr, "list_sessions", None)) else []
    active = sum(
        1 for s in sessions
        if str(getattr(s, "state", "") or (s.get("state") if isinstance(s, dict) else "")).lower()
        not in ("exited", "failed", "sessionstate.exited", "sessionstate.failed")
    )
    host = os.environ.get("AITHER_HARNESS_HOST", "127.0.0.1")
    port = os.environ.get("AITHER_HARNESS_PORT", "8362")
    harness_url = (os.environ.get("AITHER_HARNESS_URL") or f"http://{host}:{port}").rstrip("/")
    local_model_url = (
        os.environ.get("AITHER_LOCAL_MODEL_URL") or os.environ.get("AITHER_LLM_URL") or ""
    ).strip() or None
    client = genesis or SwarmGenesisClient()
    cap: Dict[str, Any] = {
        "node": socket.gethostname(),
        "harness_url": harness_url,
        "genesis_url": client.base_url,
        "swarms_endpoint": "/swarms",
        "ui_url": swarm_ui_url(),
        "harnesses_installed": installed,
        "swarm_harnesses": swarm_harnesses,
        "active_sessions": active,
        "max_sessions": MAX_SESSIONS,
        "available_slots": max(0, MAX_SESSIONS - active),
        "local_model_url": local_model_url,
        "auth_configured": bool(_internal_key() or _session_bearer()),
    }
    if probe_genesis:
        cap["genesis"] = client.probe_reachable()
    return cap


def extract_session_artifacts(text: str) -> List[Dict[str, str]]:
    """Parse ``Screenshot: <path> | <caption>`` and ``Artifact: <path> | <caption>`` lines."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for m in _ARTIFACT_LINE_RE.finditer(str(text or "")):
        path = m.group(1).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        caption = (m.group(2) or "").strip()
        kind = "screenshot" if m.group(0).lstrip().lower().startswith("screenshot") else "artifact"
        item: Dict[str, str] = {"path": path, "kind": kind}
        if caption:
            item["caption"] = caption
        out.append(item)
    return out


def extract_session_summary(events: Sequence[Dict[str, Any]], max_chars: int = 1800) -> str:
    """Build a concise summary from a harness session's recorded event stream."""
    chunks: List[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "").lower()
        if etype in ("assistant", "text", "output", "result", "done", "message", "delta"):
            txt = str(ev.get("text") or ev.get("content") or ev.get("message") or "").strip()
            if txt:
                chunks.append(txt)
        elif etype == "error":
            err = str(ev.get("error") or ev.get("message") or ev.get("text") or "").strip()
            if err:
                chunks.append(f"ERROR: {err}")
    joined = "\n".join(chunks).strip()
    if len(joined) > max_chars:
        return joined[-max_chars:]
    return joined


def _session_events(session: Any) -> List[Dict[str, Any]]:
    if hasattr(session, "recent_events") and callable(session.recent_events):
        try:
            return list(session.recent_events())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(session, "events") and callable(session.events):
        try:
            return list(session.events())
        except Exception:  # noqa: BLE001
            pass
    log_path = getattr(session, "log_path", None)
    if log_path is not None and Path(log_path).is_file():
        out: List[Dict[str, Any]] = []
        try:
            for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
        return out
    return []


def _session_state_str(session: Any) -> str:
    st = getattr(session, "state", "")
    val = getattr(st, "value", st)
    return str(val or "").strip().lower()


def _emit_harness_trace_span(
    *,
    swarm_id: str,
    agent: str,
    harness: str,
    session_id: str,
    prompt: str,
    summary: str,
    artifacts: List[Dict[str, str]],
    cwd: str,
    model_profile: str,
    thread_id: str,
    started_at: float,
    ended_at: float,
    status: str,
    error: Optional[str],
    board_seq: Optional[int],
) -> Optional[str]:
    """Fail-soft emission of one redacted ``harness`` span into ``aither.trace/v1``."""
    try:
        from lib.orchestration.swarm_trace_hooks import record_harness_span  # noqa: PLC0415

        return record_harness_span(
            swarm_id=swarm_id,
            agent=agent,
            harness=harness,
            session_id=session_id,
            prompt=prompt,
            summary=summary,
            artifacts=artifacts,
            cwd=cwd,
            model_profile=model_profile,
            thread_id=thread_id,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            error=error,
            board_seq=board_seq,
        )
    except Exception as exc:  # noqa: BLE001 - awdk standalone box may not carry AitherOS lib
        logger.debug("harness trace span skipped (fail-soft): %s", exc)
        return None


def _watch_and_report(
    session: Any,
    genesis: SwarmGenesisClient,
    *,
    swarm_id: str,
    agent: str,
    harness: str,
    prompt: str = "",
    cwd: str = "",
    model_profile: str = "",
    started_at: float = 0.0,
    thread_id: str,
    report_to: str,
    timeout_s: float,
    poll_s: float,
    result_box: Dict[str, Any],
) -> None:
    t0 = started_at or time.time()
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    final_state = _session_state_str(session)
    while time.monotonic() < deadline:
        final_state = _session_state_str(session)
        if final_state in ("exited", "failed", "idle", "done", "completed"):
            break
        time.sleep(max(0.02, poll_s))
    t1 = time.time()
    events = _session_events(session)
    summary = extract_session_summary(events)
    artifacts = extract_session_artifacts(summary)
    failed = final_state == "failed" or (final_state not in ("exited", "idle", "done", "completed"))
    if failed:
        kind = "stall"
        body = (
            f"@{report_to} [stall] Local `{harness}` harness session `{session.id}` "
            f"ended with state `{final_state}`."
            + (f"\n{summary}" if summary else "")
        )
    else:
        kind = "done"
        body = (
            f"@{report_to} [done] Local `{harness}` harness session `{session.id}` finished."
            + (f"\n{summary}" if summary else "")
        )
    try:
        posted = genesis.post_message(
            swarm_id,
            {
                "agent": agent,
                "text": body[:4000],
                "kind": kind,
                "mentions": [report_to],
                "artifacts": artifacts,
                "thread_id": thread_id,
            },
        )
        span_id = _emit_harness_trace_span(
            swarm_id=swarm_id,
            agent=agent,
            harness=harness,
            session_id=str(session.id),
            prompt=prompt,
            summary=summary,
            artifacts=artifacts,
            cwd=cwd,
            model_profile=model_profile,
            thread_id=thread_id,
            started_at=t0,
            ended_at=t1,
            status="error" if failed else "ok",
            error=f"harness ended with state={final_state}" if failed else None,
            board_seq=posted.get("seq"),
        )
        result_box.update(
            {
                "reported": True,
                "final_state": final_state,
                "kind": kind,
                "seq": posted.get("seq"),
                "span_id": span_id,
                "artifacts": artifacts,
                "summary": summary,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("swarm harness report failed swarm=%s session=%s: %s", swarm_id, session.id, exc)
        _emit_harness_trace_span(
            swarm_id=swarm_id,
            agent=agent,
            harness=harness,
            session_id=str(session.id),
            prompt=prompt,
            summary=summary,
            artifacts=artifacts,
            cwd=cwd,
            model_profile=model_profile,
            thread_id=thread_id,
            started_at=t0,
            ended_at=t1,
            status="error",
            error=str(exc),
            board_seq=None,
        )
        result_box.update({"reported": False, "final_state": final_state, "error": str(exc)})


def delegate_harness_worker(
    mgr: Any,
    genesis: SwarmGenesisClient,
    *,
    swarm_id: str,
    agent: str,
    harness: str,
    prompt: str,
    cwd: str = "",
    model_profile: str = "",
    title: str = "",
    thread_id: str = "",
    report_to: str = "coordinator",
    wait: bool = False,
    timeout_s: float = 300.0,
    poll_s: float = 0.5,
) -> Dict[str, Any]:
    """Delegate a SwarmBoard worker slot to a real local ``awsh`` harness session and
    stream its ``[done]`` summary + artifacts back onto the Swarm Board."""
    from adk.harnesses.manager import ManagerError
    from adk.harnesses.registry import get as get_spec
    from adk.harnesses.session import SessionConfig

    sid = _validate_swarm_id(swarm_id)
    h = str(harness or "").strip().lower()
    try:
        get_spec(h)
    except KeyError as exc:
        raise SwarmApiError(str(exc), status=422) from exc
    task_prompt = str(prompt or "").strip()
    if not task_prompt:
        raise SwarmApiError("prompt is required for harness delegation", status=422)
    who = str(agent or h).strip().lower() or h
    # Fail-closed auth check BEFORE spawning a local process
    genesis._auth_headers(require_auth=True)

    cfg = SessionConfig(
        harness=h,
        cwd=str(cwd or "").strip() or os.getcwd(),
        title=(title or f"swarm:{sid}:{who}")[:80],
        agent=who,
        model_profile=str(model_profile or "").strip(),
    )
    try:
        session = mgr.create(cfg)
    except ManagerError as exc:
        raise SwarmApiError(str(exc), status=422) from exc

    if hasattr(session, "send") and callable(session.send):
        try:
            session.send(task_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("initial send on session %s failed: %s", session.id, exc)

    # Record start on the Swarm Board
    start_seq = None
    try:
        started = genesis.post_message(
            sid,
            {
                "agent": who,
                "text": (
                    f"Delegated worker slot to local `{h}` harness session `{session.id}` "
                    f"on `{socket.gethostname()}`."
                ),
                "kind": "update",
                "thread_id": thread_id,
            },
        )
        start_seq = started.get("seq")
    except SwarmApiError as exc:
        logger.debug("swarm start post skipped for %s: %s", sid, exc)

    result_box: Dict[str, Any] = {}
    watcher = threading.Thread(
        target=_watch_and_report,
        kwargs={
            "session": session,
            "genesis": genesis,
            "swarm_id": sid,
            "agent": who,
            "harness": h,
            "prompt": task_prompt,
            "cwd": cfg.cwd,
            "model_profile": str(model_profile or ""),
            "started_at": time.time(),
            "thread_id": thread_id,
            "report_to": str(report_to or "coordinator").strip().lstrip("@") or "coordinator",
            "timeout_s": timeout_s,
            "poll_s": poll_s,
            "result_box": result_box,
        },
        name=f"swarm-harness-{sid}-{session.id}",
        daemon=True,
    )
    watcher.start()
    if wait:
        watcher.join(timeout=max(1.0, float(timeout_s)) + 2.0)

    out: Dict[str, Any] = {
        "ok": True,
        "swarm_id": sid,
        "session_id": str(session.id),
        "harness": h,
        "agent": who,
        "thread_id": thread_id,
        "status": _session_state_str(session) or "running",
        "start_seq": start_seq,
        "ui_url": swarm_ui_url(swarm_id=sid),
    }
    if wait:
        out["result"] = result_box
    return out


def mount(
    app: Any,
    auth: Any,
    require_owner: Any,
    *,
    mgr: Any = None,
    genesis_factory: Optional[Callable[[], SwarmGenesisClient]] = None,
) -> None:
    """Mount ``/swarms/*`` bridge + local harness delegation routes onto the ``:8362`` daemon."""
    from fastapi import Body, Depends, HTTPException, Query
    from pydantic import BaseModel, Field, ValidationError

    class LaunchBody(BaseModel):
        goal: str = Field(..., min_length=1, max_length=6000)
        title: str = Field("", max_length=120)
        coordinator: str = Field("aither", max_length=40)
        leads: Optional[List[str]] = None
        effort: int = Field(7, ge=6, le=10)
        local_only: bool = True
        work_elevated: bool = True
        auto_start: bool = True
        requested_by: str = Field("awsh:owner", max_length=80)

    class SteerBody(BaseModel):
        message: str = Field(..., min_length=1, max_length=4000)
        user: str = Field("", max_length=80)
        role: str = Field("admin", max_length=40)

    class ControlBody(BaseModel):
        action: str = Field("pause", max_length=20)
        user: str = Field("", max_length=80)
        reason: str = Field("", max_length=300)

    class DelegateHarnessBody(BaseModel):
        harness: str = Field(..., min_length=1, max_length=40)
        prompt: str = Field(..., min_length=1, max_length=12000)
        agent: str = Field("demiurge", max_length=40)
        cwd: str = Field("", max_length=500)
        model_profile: str = Field("", max_length=80)
        title: str = Field("", max_length=100)
        thread_id: str = Field("", max_length=80)
        report_to: str = Field("coordinator", max_length=40)
        wait: bool = False
        timeout_s: float = Field(300.0, ge=1.0, le=3600.0)

    def _parse(model_cls: Any, raw: Any) -> Any:
        try:
            return model_cls.model_validate(raw or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _client() -> SwarmGenesisClient:
        return genesis_factory() if genesis_factory is not None else SwarmGenesisClient()

    def _http_err(exc: SwarmApiError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    @app.get("/swarms/capacity", dependencies=[Depends(auth)])
    def swarm_capacity(probe: bool = Query(default=True)) -> Dict[str, Any]:
        return local_swarm_capacity(mgr, genesis=_client(), probe_genesis=probe)

    @app.get("/swarms", dependencies=[Depends(auth)])
    def list_swarms(
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Dict[str, Any]:
        client = _client()
        try:
            data = client.list_swarms(status=status, limit=limit)
        except SwarmApiError as exc:
            raise _http_err(exc)
        swarms = data.get("swarms") if isinstance(data.get("swarms"), list) else []
        for row in swarms:
            if isinstance(row, dict) and not row.get("ui_url"):
                row["ui_url"] = swarm_ui_url(row)
        return {
            "swarms": swarms,
            "count": int(data.get("count", len(swarms))),
            "ui_url": swarm_ui_url(),
            "capacity": local_swarm_capacity(mgr, genesis=client),
        }

    @app.get("/swarms/{swarm_id}", dependencies=[Depends(auth)])
    def get_swarm(swarm_id: str) -> Dict[str, Any]:
        client = _client()
        try:
            view = client.get_swarm(swarm_id)
        except SwarmApiError as exc:
            raise _http_err(exc)
        if isinstance(view, dict) and not view.get("ui_url"):
            view["ui_url"] = swarm_ui_url(view, swarm_id)
        return view

    @app.post("/swarms")
    def launch_swarm(
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(LaunchBody, raw)
        client = _client()
        payload = body.model_dump()
        if not payload.get("requested_by"):
            payload["requested_by"] = f"awsh:{getattr(principal, 'id', 'owner')}"
        try:
            view = client.create_swarm(payload)
        except SwarmApiError as exc:
            raise _http_err(exc)
        if isinstance(view, dict) and not view.get("ui_url"):
            view["ui_url"] = swarm_ui_url(view)
        return view

    @app.post("/swarms/{swarm_id}/steer")
    def steer_swarm(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(auth),
    ) -> Dict[str, Any]:
        body = _parse(SteerBody, raw)
        client = _client()
        who = body.user.strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        try:
            return client.steer_swarm(
                swarm_id,
                {"message": body.message, "user": who, "role": body.role, "source": "awsh"},
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/pause")
    def pause_swarm(
        swarm_id: str,
        raw: Optional[Dict[str, Any]] = Body(default=None),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(ControlBody, raw) if raw else None
        client = _client()
        who = (body.user if body else "").strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        try:
            return client.pause_swarm(
                swarm_id,
                {"action": "pause", "user": who, "reason": body.reason if body else ""},
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/control")
    def control_swarm(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(ControlBody, raw)
        client = _client()
        who = body.user.strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        try:
            return client.control_swarm(
                swarm_id,
                {"action": body.action, "user": who, "reason": body.reason},
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/delegate-harness")
    def delegate_harness(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(DelegateHarnessBody, raw)
        if mgr is None:
            raise HTTPException(status_code=503, detail="harness SessionManager unavailable")
        try:
            return delegate_harness_worker(
                mgr,
                _client(),
                swarm_id=swarm_id,
                agent=body.agent,
                harness=body.harness,
                prompt=body.prompt,
                cwd=body.cwd,
                model_profile=body.model_profile,
                title=body.title,
                thread_id=body.thread_id,
                report_to=body.report_to,
                wait=body.wait,
                timeout_s=body.timeout_s,
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

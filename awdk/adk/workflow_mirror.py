"""Workflow -> expedition mirror, host side.

Every workflow the Claude Code Workflow tool runs is mirrored as an AitherOS
expedition so Atlas, Veil and the gateway see it next to forge / swarm /
dark-factory work. Three pieces, all in this module so the hooks and the daemon
share one implementation:

1. ``parse_script_meta`` -- the PreToolUse hook reads ``export const meta``
   from the script and records it (name, description, phases, sha) under
   ``~/.aither/workflow-mirror/pending-<session>.json``. It cannot know the
   run id yet: the Workflow tool mints it when it launches.
2. ``bind_run`` -- the PostToolUse hook parses ``Run ID: wf_...`` and
   ``Transcript dir: ...`` from the tool response, joins them to the pending
   meta, records the run in ``runs.json`` and creates the expedition through
   the MCP gateway (``expedition_mirror_create``).
3. ``scan_and_mirror`` -- the tailer (``adk workflow mirror`` or the adk-serve
   background loop) reads each run's ``journal.jsonl`` from its cursor, turns
   entries into mirror events and posts them (``expedition_mirror_event``).

The journal contract, measured on 24 real runs (2026-09-10)::

    {"type": "launched"}
    {"type": "started", "key": "v2:<sha>", "agentId": "...", "label"?: "...", "phase"?: "..."}
    {"type": "result",  "key": "v2:<sha>", "agentId": "...", "result": <any JSON>}
    {"type": "failed",  "key": "v2:<sha>", "agentId": ""}

There is NO terminal entry. A run is inferred finished when every started agent
has a result or failure and the journal has been quiet for ``QUIET_SECONDS``;
the inference is stamped into the expedition metadata so nobody mistakes it
for a recorded fact.

The gateway is MCP streamable-HTTP: ``initialize`` first, then ``tools/call``
with the ``Mcp-Session-Id`` header, ``Accept`` naming both content types. A
plain JSON POST is answered 400 and an unknown tool comes back as a 200 with
``isError: false`` and the text ``Unknown tool: ...`` -- both are read as
errors here, never as success.

Stdlib only on purpose: the hooks load this file by path, without importing
the ``adk`` package (whose ``__init__`` pulls the agent runtime).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger("adk.workflow_mirror")

SOURCE = "claude-code-workflow"
DEFAULT_GATEWAY = "http://127.0.0.1:8182"
RESULT_CAP = 16_000
QUIET_SECONDS = 600
ORPHAN_SECONDS = 6 * QUIET_SECONDS
PROTOCOL_VERSION = "2025-06-18"
JOURNAL_TYPES = ("launched", "started", "result", "failed")

_RUN_ID_RE = re.compile(r"Run ID:\s*(wf_[A-Za-z0-9_-]+)")
_TRANSCRIPT_RE = re.compile(r"Transcript dir:\s*([^\r\n]+?)\s*(?:$|\n)")
_META_RE = re.compile(r"export\s+const\s+meta\s*=\s*(\{.*?\n\})", re.S)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_dir() -> Path:
    override = os.environ.get("AITHER_WORKFLOW_MIRROR_DIR")
    return Path(override) if override else Path.home() / ".aither" / "workflow-mirror"


def bearer_path() -> Path:
    return Path.home() / ".aither" / "session-bearer"


def read_bearer() -> str:
    try:
        return bearer_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Script metadata
# ---------------------------------------------------------------------------


def _js_literal_to_json(text: str) -> str:
    """Turn a JS object literal (the meta block is a PURE literal by contract)
    into JSON: strip comments and trailing commas, quote bare keys, convert
    single-quoted strings."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            buf: List[str] = []
            while j < n and text[j] != ch:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            inner = "".join(buf)
            if ch == "'":
                inner = inner.replace("\\'", "'").replace('"', '\\"')
            out.append('"' + inner + '"')
            i = j + 1
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    s = "".join(out)
    s = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', s)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def parse_script_meta(script: str) -> Dict[str, Any]:
    """``{name, description, phases: [{title, detail}]}`` from a workflow script.

    Never raises: a script with no parseable meta yields empty fields, and the
    caller labels the expedition from what it has.
    """
    empty = {"name": "", "description": "", "phases": []}
    if not script:
        return dict(empty)
    m = _META_RE.search(script)
    if not m:
        return dict(empty)
    try:
        meta = json.loads(_js_literal_to_json(m.group(1)))
    except (ValueError, TypeError):
        return dict(empty)
    if not isinstance(meta, dict):
        return dict(empty)
    return {
        "name": str(meta.get("name") or ""),
        "description": str(meta.get("description") or ""),
        "phases": normalize_phases(meta.get("phases")),
    }


def normalize_phases(raw: Any) -> List[Dict[str, str]]:
    phases: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return phases
    for i, p in enumerate(raw):
        if isinstance(p, str):
            phases.append({"title": p, "detail": ""})
        elif isinstance(p, dict):
            phases.append({"title": str(p.get("title") or f"Phase {i + 1}"),
                           "detail": str(p.get("detail") or "")})
    return phases


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def parse_run_info(response_text: str) -> Tuple[Optional[str], Optional[str]]:
    """``(run_id, transcript_dir)`` from the Workflow tool's response text."""
    run = _RUN_ID_RE.search(response_text or "")
    tdir = _TRANSCRIPT_RE.search(response_text or "")
    return (run.group(1) if run else None, tdir.group(1).strip() if tdir else None)


# ---------------------------------------------------------------------------
# Gateway client (MCP streamable-HTTP, JSON-RPC)
# ---------------------------------------------------------------------------


class MirrorError(RuntimeError):
    """The gateway answered, and the answer was not success."""


Opener = Callable[..., Any]


class GatewayClient:
    """Minimal JSON-RPC client for ``tools/call`` on the MCP gateway."""

    def __init__(
        self,
        base_url: str = DEFAULT_GATEWAY,
        bearer: Optional[str] = None,
        timeout: float = 30.0,
        opener: Optional[Opener] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer = read_bearer() if bearer is None else bearer
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._session_id: Optional[str] = None
        self._next_id = 0

    # -- transport --------------------------------------------------------

    def _post(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.base_url + "/mcp", data=json.dumps(body).encode("utf-8"), headers=headers,
        )
        try:
            with self._open(req, timeout=self.timeout) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return int(resp.status), hdrs, resp.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            return int(exc.code), {k.lower(): v for k, v in (exc.headers or {}).items()}, payload

    @staticmethod
    def _decode(raw: bytes, content_type: str) -> Dict[str, Any]:
        text = raw.decode("utf-8", "replace")
        if "text/event-stream" in content_type:
            last: Optional[Dict[str, Any]] = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        last = json.loads(line[5:].strip())
                    except ValueError:
                        continue
            if last is None:
                raise MirrorError("gateway sent an event stream with no JSON data frame")
            return last
        try:
            obj = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise MirrorError(f"gateway sent non-JSON: {text[:200]!r}") from exc
        return obj if isinstance(obj, dict) else {"result": obj}

    def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        status, hdrs, raw = self._post(body)
        if status in (401, 403):
            raise MirrorError(
                f"gateway refused the bearer ({status}); re-mint with "
                f"python AitherOS/dev/tools/mint_session_bearer.py",
            )
        if status >= 400:
            raise MirrorError(f"gateway HTTP {status}: {raw[:200]!r}")
        sid = hdrs.get("mcp-session-id")
        if sid:
            self._session_id = sid
        return self._decode(raw, hdrs.get("content-type", ""))

    # -- protocol ---------------------------------------------------------

    def initialize(self) -> Optional[str]:
        self._session_id = None
        resp = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adk-workflow-mirror", "version": "1"},
        })
        if "error" in resp:
            raise MirrorError(f"initialize failed: {resp['error']}")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except (OSError, MirrorError) as exc:
            # The notification is courtesy; the session id above is what matters.
            log.debug("notifications/initialized not accepted: %s", exc)
        return self._session_id

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call one tool and return its decoded result dict.

        Raises MirrorError on a JSON-RPC error, an ``isError`` result, an
        ``Unknown tool`` text, or a result carrying a non-empty ``error``.
        """
        if self._session_id is None:
            self.initialize()
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            if "session" in msg.lower() and self._session_id is not None:
                self.initialize()
                resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
            if "error" in resp:
                raise MirrorError(f"{name}: {resp['error']}")
        result = resp.get("result")
        if not isinstance(result, dict):
            raise MirrorError(f"{name}: no result object in {str(resp)[:200]}")
        text = ""
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                break
        if result.get("isError"):
            raise MirrorError(f"{name}: {text[:300] or 'isError'}")
        if text.startswith("Unknown tool"):
            raise MirrorError(f"{name}: {text} (fleet half not deployed or not in __all__)")
        structured = result.get("structuredContent")
        data: Any = structured if isinstance(structured, dict) else None
        if data is None:
            try:
                data = json.loads(text) if text.strip() else {}
            except ValueError:
                data = {"text": text}
        if not isinstance(data, dict):
            data = {"value": data}
        if data.get("error"):
            raise MirrorError(f"{name}: {data['error']}")
        return data


# ---------------------------------------------------------------------------
# Run discovery + journal
# ---------------------------------------------------------------------------


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def scan_runs(root: Optional[Path] = None) -> Iterator[Path]:
    """Yield every run directory holding a journal under ``root``.

    ``root`` may be the projects root, one session directory, or a
    ``workflows`` directory; a run directory itself is also accepted.
    """
    base = Path(root) if root else default_root()
    if not base.is_dir():
        return
    patterns = (
        "*/subagents/workflows/wf_*/journal.jsonl",
        "subagents/workflows/wf_*/journal.jsonl",
        "wf_*/journal.jsonl",
        "journal.jsonl",
    )
    seen = set()
    for pat in patterns:
        for j in sorted(base.glob(pat)):
            run_dir = j.parent
            if run_dir.name.startswith("wf_") and run_dir not in seen:
                seen.add(run_dir)
                yield run_dir


def read_journal(run_dir: Path) -> List[Dict[str, Any]]:
    path = Path(run_dir) / "journal.jsonl"
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    return entries


@dataclass
class RunState:
    run_id: str
    cursor: int = 0
    expedition_id: str = ""
    phase: str = ""
    open_agents: Dict[str, str] = field(default_factory=dict)
    started: int = 0
    completed: int = 0
    failed: int = 0
    finished: str = ""
    last_error: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _label_for(entry: Dict[str, Any]) -> str:
    label = entry.get("label")
    if label:
        return str(label)
    agent = entry.get("agentId")
    if agent:
        return f"agent:{agent}"
    return str(entry.get("key") or "agent")[:24]


def _cap(text: str) -> str:
    if len(text) <= RESULT_CAP:
        return text
    return text[:RESULT_CAP] + "\n... (truncated)"


def journal_to_events(entries: List[Dict[str, Any]], state: RunState) -> List[Dict[str, Any]]:
    """Turn journal entries after ``state.cursor`` into mirror events.

    Updates the state's cursor, phase pointer and agent tallies. Pure: no I/O.
    """
    events: List[Dict[str, Any]] = []
    for entry in entries[state.cursor:]:
        etype = entry.get("type")
        key = str(entry.get("key") or "")
        ts = utc_now()
        if etype == "launched":
            events.append({"type": "log", "ts": ts, "message": "launched"})
        elif etype == "started":
            label = _label_for(entry)
            phase = str(entry.get("phase") or "")
            if phase and phase != state.phase:
                state.phase = phase
                events.append({"type": "phase", "ts": ts, "phase": phase})
            state.open_agents[key or label] = label
            state.started += 1
            events.append({
                "type": "agent_started", "ts": ts, "label": label,
                "phase": phase or None, "agent_id": str(entry.get("agentId") or ""),
            })
        elif etype == "result":
            label = state.open_agents.pop(key, None) or _label_for(entry)
            result = entry.get("result")
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            state.completed += 1
            events.append({
                "type": "agent_result", "ts": ts, "label": label,
                "agent_id": str(entry.get("agentId") or ""), "result": _cap(text),
            })
        elif etype == "failed":
            label = state.open_agents.pop(key, None) or _label_for(entry)
            state.failed += 1
            events.append({
                "type": "agent_failed", "ts": ts, "label": label,
                "agent_id": str(entry.get("agentId") or ""),
                "message": "agent failed (the journal records no error text)",
            })
        else:
            events.append({
                "type": "log", "ts": ts,
                "message": json.dumps(entry, ensure_ascii=False)[:500],
            })
    state.cursor = len(entries)
    return events


def infer_terminal(
    state: RunState, journal_mtime: float, now: Optional[float] = None,
    quiet: float = QUIET_SECONDS,
) -> Optional[Dict[str, Any]]:
    """The terminal event a quiet journal implies, or None while the run may
    still be going. Never called for a run already marked finished."""
    if state.finished or state.open_agents:
        return None
    now = time.time() if now is None else now
    age = now - journal_mtime
    if state.started == 0:
        if state.failed > 0 and age >= quiet:
            return {"type": "failed", "message": "workflow failed before any agent started"}
        if age >= ORPHAN_SECONDS:
            return {"type": "failed",
                    "message": f"no agent started and the journal has been quiet {int(age)}s"}
        return None
    if age < quiet:
        return None
    summary = (f"{state.completed} agent(s) completed, {state.failed} failed; "
               f"inferred from a journal quiet for {int(age)}s (no terminal entry exists)")
    if state.completed == 0 and state.failed > 0:
        return {"type": "failed", "message": summary}
    return {"type": "completed", "result": summary}


# ---------------------------------------------------------------------------
# Bindings the hooks write
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def pending_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return state_dir() / f"pending-{safe}.json"


def write_pending(session_id: str, script: str, name_hint: str = "") -> Dict[str, Any]:
    meta = parse_script_meta(script)
    if not meta["name"]:
        meta["name"] = name_hint or "workflow"
    record = {"session_id": session_id, "sha": script_sha256(script),
              "meta": meta, "recorded_at": utc_now()}
    _write_json(pending_path(session_id), record)
    return record


def read_pending(session_id: str) -> Optional[Dict[str, Any]]:
    rec = _read_json(pending_path(session_id), None)
    return rec if isinstance(rec, dict) else None


def runs_path() -> Path:
    return state_dir() / "runs.json"


def read_runs() -> Dict[str, Dict[str, Any]]:
    data = _read_json(runs_path(), {})
    if isinstance(data, list):  # the first version wrote a list
        data = {r.get("run_id"): r for r in data if isinstance(r, dict) and r.get("run_id")}
    return data if isinstance(data, dict) else {}


def record_run(run_id: str, session_id: str, transcript_dir: str,
               pending: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    runs = read_runs()
    rec = runs.get(run_id) or {}
    rec.update({
        "run_id": run_id,
        "session_id": session_id,
        "transcript_dir": transcript_dir or rec.get("transcript_dir", ""),
        "sha": (pending or {}).get("sha", rec.get("sha", "")),
        "meta": (pending or {}).get("meta", rec.get("meta", {})),
        "recorded_at": rec.get("recorded_at") or utc_now(),
    })
    runs[run_id] = rec
    _write_json(runs_path(), runs)
    return rec


def state_path() -> Path:
    return state_dir() / "state.json"


def load_states() -> Dict[str, RunState]:
    """Cursors per run. A state file that EXISTS but will not parse is set
    aside (``state.json.corrupt-<epoch>``) and reported, never silently read
    as "no cursors": that would replay every run on the next pass and look
    like a burst of duplicate expeditions rather than a broken file."""
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, quarantine)
        except OSError:
            quarantine = path
        log.warning("workflow mirror state unreadable (%s); moved to %s and starting "
                    "from empty cursors -- the fleet side is idempotent on run id",
                    exc, quarantine)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, RunState] = {}
    for run_id, d in data.items():
        if isinstance(d, dict):
            d.setdefault("run_id", run_id)
            out[run_id] = RunState.from_dict(d)
    return out


def save_states(states: Dict[str, RunState]) -> None:
    _write_json(state_path(), {k: asdict(v) for k, v in states.items()})


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


def ensure_expedition(client: GatewayClient, run_id: str, record: Optional[Dict[str, Any]],
                      transcript_dir: str = "") -> str:
    meta = (record or {}).get("meta") or {}
    name = meta.get("name") or f"Workflow {run_id}"
    description = meta.get("description") or f"Claude Code Workflow run {run_id}"
    res = client.call("expedition_mirror_create", {
        "source": SOURCE,
        "run_id": run_id,
        "session_id": str((record or {}).get("session_id") or ""),
        "name": name,
        "description": description,
        "phases_json": json.dumps(normalize_phases(meta.get("phases"))),
        "script_sha256": str((record or {}).get("sha") or ""),
        "transcript_dir": transcript_dir or str((record or {}).get("transcript_dir") or ""),
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "localhost",
    })
    exp_id = str(res.get("expedition_id") or "")
    if not exp_id:
        raise MirrorError(f"expedition_mirror_create returned no expedition_id: {res}")
    return exp_id


def bind_run(run_id: str, session_id: str, transcript_dir: str,
             gateway_url: str = DEFAULT_GATEWAY,
             client: Optional[GatewayClient] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """PostToolUse: join the run id to its pending meta, record it, create the
    expedition. Returns ``(record, expedition_id_or_None)``; a gateway failure
    is recorded on the run so the tailer retries, never raised at the hook."""
    pending = read_pending(session_id)
    record = record_run(run_id, session_id, transcript_dir, pending)
    try:
        cli = client or GatewayClient(gateway_url)
        exp_id = ensure_expedition(cli, run_id, record, transcript_dir)
    except (MirrorError, OSError, ValueError) as exc:
        log.warning("bind_run %s: expedition not created yet: %s", run_id, exc)
        record["last_error"] = str(exc)[:300]
        runs = read_runs()
        runs[run_id] = record
        _write_json(runs_path(), runs)
        return record, None
    record["expedition_id"] = exp_id
    record.pop("last_error", None)
    runs = read_runs()
    runs[run_id] = record
    _write_json(runs_path(), runs)
    return record, exp_id


def mirror_run(client: GatewayClient, run_dir: Path, state: RunState,
               record: Optional[Dict[str, Any]], now: Optional[float] = None) -> int:
    """Post everything new for one run. Returns the number of events posted."""
    if state.finished:
        return 0
    entries = read_journal(run_dir)
    if not entries and state.cursor == 0:
        return 0
    if not state.expedition_id:
        state.expedition_id = ensure_expedition(client, state.run_id, record)
    posted = 0
    for event in journal_to_events(entries, state):
        client.call("expedition_mirror_event", {"run_id": state.run_id, "source": SOURCE, **event})
        posted += 1
    try:
        mtime = (Path(run_dir) / "journal.jsonl").stat().st_mtime
    except OSError:
        mtime = time.time() if now is None else now
    terminal = infer_terminal(state, mtime, now)
    if terminal:
        client.call("expedition_mirror_event",
                    {"run_id": state.run_id, "source": SOURCE, "ts": utc_now(), **terminal})
        state.finished = terminal["type"]
        posted += 1
    return posted


def scan_and_mirror(root: Optional[Path] = None, gateway_url: str = DEFAULT_GATEWAY,
                    client: Optional[GatewayClient] = None,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """One pass over every run under ``root``. Never raises; the summary names
    each run that failed and why, so a dark mirror is visible in the log."""
    summary: Dict[str, Any] = {"runs": 0, "events": 0, "errors": {}}
    states = load_states()
    runs = read_runs()
    cli = client
    for run_dir in scan_runs(root):
        run_id = run_dir.name
        summary["runs"] += 1
        state = states.get(run_id) or RunState(run_id=run_id)
        states[run_id] = state
        if state.finished:
            continue
        try:
            if cli is None:
                cli = GatewayClient(gateway_url)
            summary["events"] += mirror_run(cli, run_dir, state, runs.get(run_id), now)
            state.last_error = ""
        except (MirrorError, OSError, ValueError) as exc:
            state.last_error = str(exc)[:300]
            summary["errors"][run_id] = state.last_error
            log.warning("mirror %s: %s", run_id, exc)
            if isinstance(exc, MirrorError) and "bearer" in str(exc):
                break  # every further call would fail the same way
    save_states(states)
    return summary

"""Cockpit autopilot: opt-in, per-session auto-steer. OFF by default.

The cockpit could SHOW that a session finished its turn (``waiting-input``) but a
long job still stopped there until a human pressed Enter. Stall ALERTS (a session on
``blocked?`` / ``waiting-permission``) are a separate, read-only watcher that pages;
it deliberately leaves auto-steer out because steering needs an explicit opt-in. This
module is that opt-in.

When a session the owner opted in has FINISHED its turn, the watch submits a nudge
(default ``continue``). "Finished" is read from the transcript, never from the
directory's ``waiting-input`` status alone: that status is also returned when the
last line is a user-role ``tool_result``, i.e. while the model is still mid-turn.
The only turn-end signal is Claude Code's ``system``/``turn_duration`` record being
the newest conversation record (``transcript_turn_end``). Bounded on purpose:

- only DAEMON-OWNED sessions (``origin == "daemon"`` and ``steer_capability ==
  "full"``): the daemon can submit to its own ptys and nothing else;
- never on ``blocked?`` / ``waiting-permission``: that is a permission prompt, and
  typing into it would approve tools on the owner's behalf. Autopilot continues
  finished turns; it never answers prompts;
- at most ONE nudge per finished turn: the turn-end record steered is remembered
  (``last_turn_end``) and the same record is never steered twice, so a nudge whose
  line has not reached the transcript yet is not re-sent;
- at most ``max_steers`` nudges per enablement and one per ``min_interval_s``.
  These two are the bound on a session that answers ``continue`` with an empty
  turn: every nudge produces a new turn end, so the per-turn rule alone does not
  stop it. Reaching the cap turns autopilot off.

State lives in ``~/.aither/autopilot.json`` (``AITHER_AUTOPILOT_FILE`` overrides).
An absent entry is OFF, and with no entry at all the watch never lists sessions.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How often the watch looks. A finished turn waiting 15 s more costs nothing.
WATCH_INTERVAL_SECONDS = 15.0

#: The directory status a finished turn shows. NECESSARY, not sufficient: the
#: directory also returns it mid-turn (last line a user-role tool_result), so a
#: steer additionally needs ``transcript_turn_end``.
IDLE_TURN_STATUS = "waiting-input"

#: How much of the transcript tail the turn-end check reads.
TURN_END_TAIL_BYTES = 256 * 1024

#: Records that are bookkeeping, not conversation: they may follow a turn end.
_NON_CONVERSATION_SYSTEM_SUBTYPES = ("away_summary",)

AUTOPILOT_DEFAULT_TEXT = "continue"
AUTOPILOT_DEFAULT_MAX_STEERS = 20
AUTOPILOT_DEFAULT_MIN_INTERVAL = 120.0
#: Hard ceilings a caller cannot configure past.
AUTOPILOT_MAX_STEERS_CEILING = 200
AUTOPILOT_MIN_INTERVAL_FLOOR = 30.0
AUTOPILOT_TEXT_MAX = 2000

#: Signature of the steer sink: (session_id, text) -> accepted?
SubmitFn = Callable[[str, str], bool]
#: Signature of the turn-end reader: row -> marker of the finished turn, or None.
TurnEndFn = Callable[[Dict[str, Any]], Optional[str]]


def transcript_turn_end(transcript_path: str) -> Optional[str]:
    """Marker of the turn that just FINISHED, or None when no turn has finished.

    A turn has finished only when the newest conversation record (``user`` /
    ``assistant`` / ``system``) is ``system``/``turn_duration``. A trailing
    ``user`` record -- a typed prompt or a ``tool_result`` -- means the model is
    working; a trailing ``assistant`` record means it is generating. Other record
    types (summaries, snapshots, titles) are skipped. Any read error is None:
    fail closed, never steer on a guess. The marker is the record's ``uuid`` (or
    its timestamp) so the caller can tell one finished turn from the next.
    """
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TURN_END_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind not in ("user", "assistant", "system"):
            continue
        if kind == "system":
            subtype = obj.get("subtype")
            if subtype in _NON_CONVERSATION_SYSTEM_SUBTYPES:
                continue
            if subtype == "turn_duration":
                marker = obj.get("uuid") or obj.get("timestamp") or ""
                return str(marker) or f"turn@{size}"
        return None
    return None


def _row_turn_end(row: Dict[str, Any]) -> Optional[str]:
    return transcript_turn_end(str(row.get("transcript_path") or ""))


def autopilot_path() -> Path:
    override = os.environ.get("AITHER_AUTOPILOT_FILE", "")
    if override:
        return Path(override)
    return Path.home() / ".aither" / "autopilot.json"


class Autopilot:
    """Per-session opt-in auto-steer. Absent from the store = OFF."""

    def __init__(
        self,
        path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
        turn_end: TurnEndFn = _row_turn_end,
    ) -> None:
        self._path = Path(path) if path else autopilot_path()
        self._clock = clock
        self._turn_end = turn_end
        self._lock = threading.Lock()

    # -- store ---------------------------------------------------------------

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def any_enabled(self) -> bool:
        with self._lock:
            return any(isinstance(e, dict) and e.get("enabled") for e in self._load().values())

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._load().get(session_id)
        if not isinstance(entry, dict):
            return {"session_id": session_id, "enabled": False}
        out = dict(entry)
        out["session_id"] = session_id
        out["enabled"] = bool(entry.get("enabled"))
        return out

    def configure(
        self,
        session_id: str,
        *,
        enabled: bool,
        text: str = AUTOPILOT_DEFAULT_TEXT,
        max_steers: int = AUTOPILOT_DEFAULT_MAX_STEERS,
        min_interval_s: float = AUTOPILOT_DEFAULT_MIN_INTERVAL,
        by: str = "",
    ) -> Dict[str, Any]:
        """Enable (resetting the counter) or disable autopilot for one session."""
        if not session_id:
            raise ValueError("session_id required")
        text = (text or "").strip() or AUTOPILOT_DEFAULT_TEXT
        if len(text) > AUTOPILOT_TEXT_MAX:
            raise ValueError(f"text longer than {AUTOPILOT_TEXT_MAX} chars")
        max_steers = max(1, min(int(max_steers), AUTOPILOT_MAX_STEERS_CEILING))
        min_interval_s = max(float(min_interval_s), AUTOPILOT_MIN_INTERVAL_FLOOR)
        with self._lock:
            data = self._load()
            if not enabled:
                data.pop(session_id, None)
            else:
                data[session_id] = {
                    "enabled": True,
                    "text": text,
                    "max_steers": max_steers,
                    "min_interval_s": min_interval_s,
                    "steers": 0,
                    "last_steer_at": 0.0,
                    "last_turn_end": "",
                    "enabled_at": self._clock(),
                    "enabled_by": by,
                }
            self._save(data)
        return self.get(session_id)

    # -- the tick ------------------------------------------------------------

    def tick(self, rows: List[Dict[str, Any]], submit: SubmitFn) -> List[str]:
        """Steer every opted-in daemon session whose turn has finished.

        Returns the ids steered this call.
        """
        with self._lock:
            data = self._load()
            if not data:
                return []
            now = self._clock()
            steered: List[str] = []
            changed = False
            live = {str(r.get("id") or ""): r for r in rows}
            for sid, entry in list(data.items()):
                if not isinstance(entry, dict) or not entry.get("enabled"):
                    continue
                row = live.get(sid)
                if row is None:
                    continue
                if row.get("status") == "exited":
                    data.pop(sid, None)
                    changed = True
                    continue
                # Only a finished turn, only a pty this daemon owns. Never a prompt.
                if row.get("origin") != "daemon" or row.get("status") != IDLE_TURN_STATUS:
                    continue
                if row.get("steer_capability") != "full":
                    continue
                if now - float(entry.get("last_steer_at") or 0.0) < float(
                    entry.get("min_interval_s") or AUTOPILOT_DEFAULT_MIN_INTERVAL
                ):
                    continue
                # The real turn-end signal: waiting-input is also the mid-turn
                # state between a tool_result and the next assistant block.
                turn = self._turn_end(row)
                if not turn:
                    continue
                if turn == entry.get("last_turn_end"):
                    continue  # this finished turn was already nudged
                try:
                    ok = bool(submit(sid, str(entry.get("text") or AUTOPILOT_DEFAULT_TEXT)))
                except Exception as exc:  # noqa: BLE001 -- one bad pty must not stop the rest
                    logger.debug("autopilot submit to %s raised: %s", sid, exc)
                    ok = False
                if not ok:
                    continue
                entry["steers"] = int(entry.get("steers", 0)) + 1
                entry["last_steer_at"] = now
                entry["last_turn_end"] = turn
                if entry["steers"] >= int(entry.get("max_steers") or AUTOPILOT_DEFAULT_MAX_STEERS):
                    entry["enabled"] = False
                    entry["disabled_reason"] = "max_steers reached"
                changed = True
                steered.append(sid)
            if changed:
                self._save(data)
            return steered


class AutopilotWatch:
    """Background thread: feeds directory snapshots to the autopilot."""

    def __init__(
        self,
        list_rows: Callable[[], List[Dict[str, Any]]],
        submit: SubmitFn,
        autopilot: Optional[Autopilot] = None,
        interval: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        self._list_rows = list_rows
        self._submit = submit
        self.autopilot = autopilot or Autopilot()
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> List[str]:
        # Nobody opted in (the default): never pay for a directory listing.
        if not self.autopilot.any_enabled():
            return []
        return self.autopilot.tick(self._list_rows(), self._submit)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cockpit-autopilot", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # A dead watch is an autopilot that silently stopped: say so.
                sys.stderr.write(f"[cockpit-autopilot] tick failed: {exc}\n")
            self._stop.wait(self.interval)

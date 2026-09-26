"""Live sink: stream an ARC-AGI-3 run to the ARC Theater's ``/api/ingest``.

    ADK_ARC_LIVE_URL=https://arc.aitherium.com  adk eval arc --games ls20 --cap 60
    adk solve --env arc --game ls20 --live-url http://127.0.0.1:28198

The Theater (``arc_live_server.py``) shows the posted run beside its own solver:
the board, the turn's SASE phases, intent, PRISM strategy, prediction hit/miss,
and the hypothesis list.

Contract: the sink must NEVER slow or break a run. Every post goes onto a
bounded queue that a daemon thread drains; a full queue DROPS the event, a
failed POST is counted, nothing raises into the caller. Consecutive failures
back the worker off so a dead Theater costs one timeout per backoff window, not
one per action. Batches coalesce frames: only the newest step in a batch keeps
its 64x64 frame, so a fast policy costs one frame per POST, not one per action.

Auth: the Theater's shared token, from ``ADK_ARC_LIVE_TOKEN`` or
``ARC_THEATER_INGEST_TOKEN``. No token = the sink is disabled with one line on
stderr (the Theater refuses unauthenticated writes anyway).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

_log = logging.getLogger(__name__)

__all__ = [
    "LiveSink",
    "LiveEnv",
    "LoopTee",
    "from_args",
    "instrument_suite",
    "tee_session",
    "URL_ENV",
    "TOKEN_ENVS",
]

URL_ENV = "ADK_ARC_LIVE_URL"
TOKEN_ENVS = ("ADK_ARC_LIVE_TOKEN", "ARC_THEATER_INGEST_TOKEN")
_OFF = ("", "0", "off", "none", "false", "no")


def _ingest_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/api/ingest") else url + "/api/ingest"


class LiveSink:
    """Fire-and-forget event poster. ``post`` never blocks and never raises."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        queue_max: int = 1024,
        batch_max: int = 48,
        timeout_s: float = 3.0,
        backoff_s: float = 10.0,
        opener: Optional[Callable[[str, bytes, Dict[str, str], float], int]] = None,
    ) -> None:
        self.url = _ingest_url(url)
        self._token = token
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=queue_max)
        self.batch_max = batch_max
        self.timeout_s = timeout_s
        self.backoff_s = backoff_s
        self._opener = opener or self._urlopen
        self.sent = 0
        self.dropped = 0
        self.failed = 0
        self.last_error: Optional[str] = None
        self._fail_streak = 0
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._worker, name="arc-live-sink", daemon=True)
        self._thread.start()

    # -- producer side ------------------------------------------------------
    def post(self, event: Dict[str, Any]) -> None:
        try:
            self._idle.clear()
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - the sink never raises into a run
            self.dropped += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "sent": self.sent,
            "dropped": self.dropped,
            "failed": self.failed,
            "queued": self._q.qsize(),
            "last_error": self.last_error,
        }

    def close(self, timeout_s: float = 5.0) -> Dict[str, Any]:
        """Flush what is queued (bounded wait), stop the worker, return stats."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline and not (self._q.empty() and self._idle.is_set()):
            time.sleep(0.05)
        self._stop.set()
        self._thread.join(timeout=max(0.1, deadline - time.monotonic()))
        return self.stats()

    # -- worker side --------------------------------------------------------
    def _urlopen(self, url: str, body: bytes, headers: Dict[str, str], timeout: float) -> int:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        ctx = (
            ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
            if url.startswith("https://")
            else None
        )
        opener = (
            urllib.request.build_opener(
                urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
            )
            if ctx is not None
            else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        )
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)

    def _drain(self, first: Dict[str, Any]) -> List[Dict[str, Any]]:
        batch = [first]
        while len(batch) < self.batch_max:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        last_step = max((i for i, e in enumerate(batch) if e.get("type") == "step"), default=-1)
        for i, e in enumerate(batch):
            if e.get("type") == "step" and i != last_step and "frame" in e:
                e = dict(e)
                e.pop("frame", None)
                batch[i] = e
        return batch

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=0.2)
            except queue.Empty:
                self._idle.set()
                continue
            batch = self._drain(first)
            if self._fail_streak >= 3 and time.monotonic() < getattr(self, "_retry_at", 0.0):
                self.dropped += len(batch)  # backing off: a dead Theater costs nothing
                continue
            try:
                body = json.dumps({"events": batch}, separators=(",", ":"), default=str).encode()
                code = self._opener(
                    self.url,
                    body,
                    {"Content-Type": "application/json", "Authorization": "Bearer " + self._token},
                    self.timeout_s,
                )
                if 200 <= code < 300:
                    self.sent += len(batch)
                    self._fail_streak = 0
                else:
                    raise RuntimeError("HTTP %d" % code)
            except Exception as exc:  # noqa: BLE001 - counted, never raised
                self.failed += len(batch)
                self._fail_streak += 1
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                if self._fail_streak >= 3:
                    self._retry_at = time.monotonic() + self.backoff_s


# ---------------------------------------------------------------------------
# environment wrapper: one ``step`` event per action
# ---------------------------------------------------------------------------
def _frame_list(state: Any) -> Optional[List[List[int]]]:
    try:
        grid = state.tolist() if hasattr(state, "tolist") else state
        if isinstance(grid, list) and grid and isinstance(grid[0], list):
            if grid[0] and isinstance(grid[0][0], list):  # a layer stack: newest layer
                grid = grid[-1]
            return [[int(c) for c in row] for row in grid[:64]]
    except Exception:  # noqa: BLE001
        return None
    return None


class LiveEnv:
    """Wraps an Environment; posts a ``step`` after every ``act``. Everything else
    (hooks, ledger properties) is delegated, so the loop sees the same env."""

    def __init__(self, env: Any, sink: LiveSink, game: str, seed: int = 0) -> None:
        self._env = env
        self._live_sink = sink
        self._live_game = game
        self._live_seed = seed

    def observe(self) -> Any:
        return self._env.observe()

    def act(self, action: Any, source: str = "model") -> Any:
        obs = self._env.act(action, source=source)
        try:
            a = (action, -1, -1) if isinstance(action, int) else tuple(action) + (-1, -1)
            aid, x, y = int(a[0]), int(a[1]), int(a[2])
            env = self._env
            self._live_sink.post(
                {
                    "type": "step",
                    "game": self._live_game,
                    "seed": self._live_seed,
                    "level": int(getattr(obs, "level", 0) or 0),
                    "action": "RESET" if aid == 0 else "ACTION%d" % aid,
                    "xy": [x, y] if x >= 0 and y >= 0 else None,
                    "frame": _frame_list(getattr(obs, "state", None)),
                    "levels_completed": int(getattr(env, "levels", getattr(obs, "level", 0)) or 0),
                    "actions": int(getattr(env, "actions", 0) or 0),
                    "state": str(getattr(env, "state_name", "") or ""),
                    "source": source,
                }
            )
        except Exception:  # noqa: BLE001 - the sink never breaks a run
            _log.debug("live sink failed; the run continues without it", exc_info=True)
        return obs

    def available_actions(self) -> Any:
        return self._env.available_actions()

    def done(self) -> bool:
        return self._env.done()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)


# ---------------------------------------------------------------------------
# reasoning-loop records -> turn / hypothesis events
# ---------------------------------------------------------------------------
_EXPECT_RE = re.compile(r"expect\s*=\s*(.+?)\)?\s*$", re.M)
_REFUTED_RE = re.compile(r"^REFUTED by new evidence:\s*(.+)$", re.M)
_MISSES_RE = re.compile(r"^Recent prediction misses:\s*(.+)$", re.M)


class LoopTee:
    """Maps the loop's sink records (``turn``/``result``) onto Theater events."""

    def __init__(self, sink: LiveSink, game: str) -> None:
        self.sink = sink
        self.game = game
        self._last_misses = ""
        self._verified: set = set()

    def __call__(self, rec: Mapping[str, Any]) -> None:
        try:
            self._map(rec)
        except Exception:  # noqa: BLE001 - never raise into the loop
            _log.debug("live record mapping failed; the loop continues", exc_info=True)

    def _map(self, rec: Mapping[str, Any]) -> None:
        kind = rec.get("event")
        if kind == "turn":
            from adk.reasoning.solve._vendor.sase import parse_reply

            content = str(rec.get("content") or "")
            parsed = parse_reply(content)
            sase = {
                k.lower(): re.sub(r"^[ \t>*#]*\**%s\**[ \t]*:?" % k, "", v, flags=re.I).strip()
                for k, v in parsed.phases.items()
            }
            code = parsed.code
            preds = [m.group(1).strip() for m in _EXPECT_RE.finditer(code)][:3]
            self.sink.post(
                {
                    "type": "turn",
                    "game": self.game,
                    "turn": rec.get("turn"),
                    "intent": rec.get("intent"),
                    "strategy": rec.get("strategy"),
                    "sase": sase,
                    "code": code[:1500],
                    "prediction": " | ".join(preds) or None,
                }
            )
        elif kind == "result":
            result = str(rec.get("result") or "")
            m = _MISSES_RE.search(result)
            misses = m.group(1) if m else ""
            hit: Optional[bool] = None
            if misses and misses != self._last_misses:
                hit = False
            elif rec.get("success"):
                hit = True
            self._last_misses = misses or self._last_misses
            self.sink.post(
                {
                    "type": "turn",
                    "game": self.game,
                    "turn": rec.get("turn"),
                    "outcome": result,
                    "hit": hit,
                }
            )
            for name in rec.get("verified_now") or []:
                if name not in self._verified:
                    self._verified.add(name)
                    self.sink.post(
                        {
                            "type": "hypothesis",
                            "name": name,
                            "status": "verified",
                            "evidence": "verified at turn %s" % rec.get("turn"),
                        }
                    )
            r = _REFUTED_RE.search(result)
            if r:
                for name in [n.strip() for n in r.group(1).split(",") if n.strip()]:
                    self._verified.discard(name)
                    self.sink.post(
                        {
                            "type": "hypothesis",
                            "name": name,
                            "status": "refuted",
                            "evidence": misses or "refuted at turn %s" % rec.get("turn"),
                        }
                    )
        elif kind == "llm_error":
            self.sink.post(
                {
                    "type": "turn",
                    "game": self.game,
                    "turn": rec.get("turn"),
                    "outcome": "model error: %s" % rec.get("error"),
                    "hit": None,
                }
            )


def tee_session(session: Any, tee: LoopTee) -> Any:
    """Make ``session.emit`` also feed ``tee``. Returns the session."""
    orig = session.emit

    def emit(event: Any) -> None:
        orig(event)
        if isinstance(event, dict):
            tee(event)

    session.emit = emit
    return session


def new_session(tee: LoopTee) -> Any:
    """A fresh ReasoningSession for one SolveRun, tee'd to the Theater."""
    from adk.reasoning_session import get_session_manager

    return tee_session(get_session_manager().get_or_create("solve-" + uuid.uuid4().hex[:12]), tee)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def _token() -> str:
    for name in TOKEN_ENVS:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return ""


def from_args(
    live_url: Optional[str],
    *,
    model: str = "",
    mode: str = "",
    policy: str = "",
    games: Sequence[str] = (),
    run_id: Optional[str] = None,
    say: Callable[..., Any] = lambda *a: print(*a, file=sys.stderr),
) -> Optional[LiveSink]:
    """A started sink (with ``run_start`` posted), or None when disabled.

    ``live_url`` beats ``$ADK_ARC_LIVE_URL``; ``off`` disables even with the env set.
    """
    url = live_url if live_url is not None else os.environ.get(URL_ENV, "")
    if (url or "").strip().lower() in _OFF:
        return None
    token = _token()
    if not token:
        say("live sink disabled: set %s (or %s) to the Theater ingest token" % TOKEN_ENVS)
        return None
    sink = LiveSink(url, token)
    sink.post(
        {
            "type": "run_start",
            "run_id": run_id or "adk-" + uuid.uuid4().hex[:10],
            "model": model or policy,
            "mode": mode or policy,
            "policy": policy,
            "games": list(games),
        }
    )
    say("live: posting to %s" % sink.url)
    return sink


def finish(
    sink: Optional[LiveSink], say: Callable[..., Any] = lambda *a: print(*a, file=sys.stderr)
) -> None:
    if sink is None:
        return
    sink.post({"type": "run_end"})
    st = sink.close()
    say(
        "live: sent=%d dropped=%d failed=%d%s"
        % (
            st["sent"],
            st["dropped"],
            st["failed"],
            (" last_error=%s" % st["last_error"]) if st["last_error"] else "",
        )
    )


def score_event(
    game: str,
    seed: int,
    level_actions: Sequence[int],
    baseline: Sequence[int],
    actions: int,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    from . import rhae

    try:
        score: Optional[float] = (
            rhae.game_rhae(list(level_actions), list(baseline)) if baseline else None
        )
    except ValueError:
        score = None
    return {
        "type": "score",
        "game": game,
        "seed": seed,
        "rhae": score,
        "levels": len(level_actions),
        "actions": actions,
        "reason": reason,
    }


def instrument_suite(
    policy: Callable[..., Any],
    make_env: Optional[Callable[[str, int], Any]],
    sink: LiveSink,
    *,
    env_dir: Optional[str] = None,
) -> Any:
    """``(policy, make_env)`` for ``run_suite`` that stream every episode to ``sink``."""
    if make_env is None:
        from .env_arc import ArcAgi3Environment

        def make_env(game: str, seed: int) -> Any:
            return ArcAgi3Environment(game, seed, env_dir=env_dir)

    base_make = make_env

    def live_make(game: str, seed: int) -> Any:
        return LiveEnv(base_make(game, seed), sink, game, seed)

    def live_policy(env: Any, ctx: Any) -> Any:
        reason = None
        try:
            return policy(env, ctx)
        except BaseException as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise
        finally:
            try:
                sink.post(
                    score_event(
                        ctx.game,
                        ctx.seed,
                        list(getattr(env, "level_actions", []) or []),
                        ctx.baseline,
                        int(getattr(env, "actions", 0) or 0),
                        reason,
                    )
                )
            except Exception:  # noqa: BLE001
                _log.debug("live score event failed; the run result stands", exc_info=True)

    live_policy.__name__ = getattr(policy, "__name__", "policy")
    return live_policy, live_make

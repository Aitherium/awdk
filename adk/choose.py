"""adk.choose -- one call for a bounded decision, and one call to teach it.

    from adk.choose import decide, outcome
    d = decide("router", "kind:code,len:long", options=["fast", "deep"])
    ...run the branch d.answer picked, then verify fresh state...
    outcome(d.decision_id, reward=+1.0 if it_worked else -1.0)

`decide` asks the AitherOS decision door (the world model's `/decide`) for a
choice / score / yesno. The door answers from what it has LEARNED about this fork
when it can (microseconds, no model call), from a local model when it cannot,
and says which it did (`source`) with a confidence that means something.
`outcome` is the half the door lives on: it turns the decision you just acted on
into training signal, so the next identical state is answered from evidence.

Endpoint: AITHER_DECIDE_URL (default https://127.0.0.1:8197, the in-fleet door;
set it to your gateway's https://.../v1 when calling from outside). Domains are
namespaced `decide.<fork>` automatically.

**No door? There is a local backend.** With the optional package `awdecide`
installed (`pip install "awdk[decide]"`) `decide`, `decide_batch`, `outcome` and
`teach` are answered IN-PROCESS -- same evidence-then-model ladder, same
learning from outcomes, a sqlite ledger at awdecide's own default path
(`AWDECIDE_DB`), and an optional brain from `AWDECIDE_LLM_URL` +
`AWDECIDE_LLM_MODEL`. A local answer always says so: `source` is
`local:<backend>` (`local:evidence`, `local:chat`, `local:none`), never plain
`engine`/`llm`, so a caller can always tell it did not come from the door.

It engages when you named no door and the default one is unreachable, or when
you set `AITHER_DECIDE_URL=local` (which skips the network entirely). If you
NAMED a door, an outage stays an outage -- you are owed an honest error, not a
quietly different answer; `AITHER_DECIDE_LOCAL=1` overrides that, `=0` disables
the local backend outright. Without `awdecide` the behaviour is unchanged:
`DecideUnavailableError`, whose message now names the fix.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_URL = "https://127.0.0.1:8197"


class DecideUnavailableError(RuntimeError):
    """The door did not answer. Callers fall back to their own default branch;
    they must not treat this as a decision."""


@dataclass
class Decision:
    decision_id: str
    answer: Any
    confidence: float
    source: str
    learned_from: int = 0
    latency_ms: float = 0.0
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def learned(self) -> bool:
        """Answered from resolved outcomes rather than a model -- at the door
        (`engine`) or in-process (`local:evidence`)."""
        return self.source == "engine" or self.source == "local:evidence"


def _url() -> str:
    return os.environ.get("AITHER_DECIDE_URL", DEFAULT_URL).rstrip("/")


def _ctx(url: str) -> Optional[ssl.SSLContext]:
    if not url.startswith("https"):
        return None
    ctx = ssl.create_default_context()
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and os.path.isfile(bundle):
        ctx.load_verify_locations(bundle)
    elif url.startswith("https://127.0.0.1") or url.startswith("https://localhost"):
        # the in-fleet door serves the internal CA; a loopback caller without the
        # bundle still gets an encrypted hop, not a refusal
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post(path: str, body: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    url = _url()
    headers = {"Content-Type": "application/json"}
    tok = os.environ.get("AITHER_DECIDE_TOKEN") or os.environ.get("AITHER_WM_INTERNAL_TOKEN")
    if tok:
        headers["X-WM-Token"] = tok
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx(url)) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 422:
            raise ValueError(f"decide refused the request: {detail}") from None
        raise DecideUnavailableError(f"decide door HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DecideUnavailableError(f"decide door unreachable at {url}: {e}") from None


def _domain(fork: str) -> str:
    fork = fork.strip()
    return fork if fork.startswith("decide.") else f"decide.{fork}"


def _shape(d: Dict[str, Any]) -> Decision:
    return Decision(
        decision_id=str(d.get("decision_id", "")),
        answer=d.get("answer"),
        confidence=float(d.get("confidence", 0.0) or 0.0),
        source=str(d.get("source", "none")),
        learned_from=int(d.get("learned_from", 0) or 0),
        latency_ms=float(d.get("latency_ms", 0.0) or 0.0),
        alternatives=list(d.get("alternatives") or []),
        raw=d,
    )


# --------------------------------------------------------------------- local
# The door is one implementation of this contract; `awdecide` is another, small
# enough to run in this process. Everything below is inert until a local answer
# is actually needed -- importing this module never imports awdecide.

LOCAL = "local"
_INSTALL_HINT = 'no local decision backend either: pip install "awdk[decide]"'
_EVIDENCE_N = re.compile(r"evidence:\s*(\d+)\s+resolved")
# decision ids answered locally in THIS process, so outcome() routes them back
# to the ledger that issued them. Bounded: past the cap the door is tried first
# and the local ledger is still reached through the normal fallback.
_LOCAL_IDS: Set[str] = set()
_LOCAL_IDS_MAX = 4096


def _import_awdecide() -> Optional[Tuple[Any, Any]]:
    """The one guarded, lazy import point for the local backend.

    Returns `(mcp, ledger)` from awdecide, or None when it is not installed.
    Tests replace this function to simulate either."""
    try:
        from awdecide import ledger as ledger_mod
        from awdecide import mcp as mcp_mod
    except ImportError:
        return None
    return mcp_mod, ledger_mod


def _raw_url() -> str:
    return os.environ.get("AITHER_DECIDE_URL", "").strip()


def _local_only() -> bool:
    """AITHER_DECIDE_URL=local -- answer here, never touch the network."""
    return _raw_url().lower() == LOCAL


def _local_allowed() -> bool:
    """May a door outage fall through to the local backend?"""
    flag = os.environ.get("AITHER_DECIDE_LOCAL", "").strip().lower()
    if flag in ("0", "off", "no", "false", "never"):
        return False
    if flag in ("1", "on", "yes", "true", "always"):
        return True
    return not _raw_url()


def _local_loop(door_down: Optional[Exception] = None) -> Tuple[Any, Any]:
    """`(mcp, Loop)` -- a FRESH Loop per call, because its sqlite connection
    must not be shared across threads. The caller closes the ledger."""
    mods = _import_awdecide()
    if mods is None:
        raise DecideUnavailableError(f"{door_down}; {_INSTALL_HINT}" if door_down
                                     else _INSTALL_HINT) from None
    mcp_mod, ledger_mod = mods
    db = os.environ.get("AWDECIDE_DB")
    return mcp_mod, mcp_mod.build_loop(Path(db) if db else ledger_mod.DEFAULT_DB)


def _local_args(fork: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """This client's vocabulary in awdecide's terms. The mapping (yesno -> bool,
    decision_id <- id, answer <- value, source <- backend) is awdecide's own
    `mcp.call`; only the argument names are ours."""
    return {
        "fork": _domain(fork),
        "state": body.get("state", ""),
        "kind": body.get("kind", "choice"),
        "options": body.get("options"),
        "question": body.get("question", ""),
        "min_confidence": body.get("min_confidence", 0.0),
    }


def _shape_local(a: Dict[str, Any]) -> Decision:
    """An awdecide answer as a Decision. An undecided answer is `answer=None`,
    confidence 0.0, source `local:none` -- never a guess."""
    decided = bool(a.get("decided"))
    learned_from = 0
    for reason in a.get("reasons") or []:
        hit = _EVIDENCE_N.search(str(reason))
        if hit:
            learned_from = int(hit.group(1))
    probs = dict(a.get("probabilities") or {})
    d = Decision(
        decision_id=str(a.get("decision_id") or ""),
        answer=a.get("answer") if decided else None,
        confidence=float(a.get("confidence") or 0.0) if decided else 0.0,
        source=f"local:{a.get('source') or 'none'}" if decided else "local:none",
        learned_from=learned_from,
        alternatives=[{"answer": k, "value": v}
                      for k, v in sorted(probs.items(), key=lambda kv: -kv[1])],
        raw=a,
    )
    if d.decision_id:
        if len(_LOCAL_IDS) >= _LOCAL_IDS_MAX:
            _LOCAL_IDS.clear()
        _LOCAL_IDS.add(d.decision_id)
    return d


def _local_decide(fork: str, body: Dict[str, Any], down: Optional[Exception] = None) -> Decision:
    mcp_mod, loop = _local_loop(down)
    try:
        return _shape_local(mcp_mod.call(loop, "decide", _local_args(fork, body)))
    finally:
        loop.ledger.close()


def _local_batch(
    fork: str, items: Sequence[Dict[str, Any]], down: Optional[Exception] = None
) -> List[Decision]:
    """One Loop, one connection, one thread; order preserved."""
    mcp_mod, loop = _local_loop(down)
    try:
        return [
            _shape_local(mcp_mod.call(loop, "decide",
                                      _local_args(str(it.get("fork") or fork), dict(it))))
            for it in items
        ]
    finally:
        loop.ledger.close()


def _local_outcome(
    decision_id: str, reward: float, down: Optional[Exception] = None
) -> Dict[str, Any]:
    """Route to `Loop.resolve`. An id the ledger never issued raises ValueError,
    the same way the door's 422 does."""
    mcp_mod, loop = _local_loop(down)
    try:
        return mcp_mod.call(loop, "decide_outcome",
                            {"decision_id": decision_id, "correct": float(reward) > 0})
    finally:
        loop.ledger.close()


def _local_teach(
    fork: str, state: str, answer: Any, reward: float, down: Optional[Exception] = None
) -> Dict[str, Any]:
    mcp_mod, loop = _local_loop(down)
    try:
        return mcp_mod.call(loop, "decide_teach", {"fork": _domain(fork), "state": state,
                                                   "value": str(answer),
                                                   "correct": float(reward) > 0})
    finally:
        loop.ledger.close()


def decide(
    fork: str,
    state: str,
    *,
    options: Optional[Sequence[Any]] = None,
    kind: str = "choice",
    question: str = "",
    min_confidence: float = 0.0,
    timeout: float = 30.0,
) -> Decision:
    """Ask the door. `state` must be a STABLE descriptor of the situation at this
    fork (same situation -> same string); that is what lets it learn."""
    body = {
        "domain": _domain(fork),
        "state": state,
        "kind": kind,
        "question": question,
        "min_confidence": min_confidence,
    }
    if options is not None:
        body["options"] = [str(o) if kind != "score" or options else o for o in options]
    if _local_only():
        return _local_decide(fork, body)
    try:
        return _shape(_post("/decide", body, timeout))
    except DecideUnavailableError as down:
        if not _local_allowed():
            raise
        return _local_decide(fork, body, down)


def decide_batch(
    fork: str, items: Sequence[Dict[str, Any]], *, timeout: float = 60.0
) -> List[Decision]:
    """Several decisions in one round trip. Each item: {state, options?, kind?,
    question?, min_confidence?}. Order is preserved."""
    payload = [
        {
            "domain": _domain(str(it.get("fork") or fork)),
            "state": it["state"],
            "kind": it.get("kind", "choice"),
            "options": it.get("options"),
            "question": it.get("question", ""),
            "min_confidence": it.get("min_confidence", 0.0),
        }
        for it in items
    ]
    if _local_only():
        return _local_batch(fork, items)
    try:
        return [
            _shape(a)
            for a in _post("/decide/batch", {"items": payload}, timeout).get("answers", [])
        ]
    except DecideUnavailableError as down:
        if not _local_allowed():
            raise
        return _local_batch(fork, items, down)


def outcome(decision_id: str, reward: float, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Teach the door: reward in -1..1 for the decision you acted on. An id the
    local backend issued goes back to it, wherever the door is."""
    did = str(decision_id)
    if _local_only() or did in _LOCAL_IDS:
        return _local_outcome(did, reward)
    try:
        return _post("/decide/outcome", {"decision_id": did, "reward": float(reward)}, timeout)
    except DecideUnavailableError as down:
        if not _local_allowed():
            raise
        return _local_outcome(did, reward, down)


def teach(
    fork: str, state: str, answer: Any, reward: float, *, timeout: float = 15.0
) -> Dict[str, Any]:
    """Teach without a prior decision: 'in this state, this answer earned this'."""
    body = {"domain": _domain(fork), "state": state, "answer": answer, "reward": float(reward)}
    if _local_only():
        return _local_teach(fork, state, answer, reward)
    try:
        return _post("/decide/outcome", body, timeout)
    except DecideUnavailableError as down:
        if not _local_allowed():
            raise
        return _local_teach(fork, state, answer, reward, down)


def judge(
    output: str,
    criteria: Sequence[str],
    *,
    fork: str = "judge",
    min_confidence: float = 0.0,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Grade unstructured output against criteria -- the eval-judge shape.

        r = judge(run_log, ["the tests passed", "no traceback"], fork="ci")
        for v in r["verdicts"]:
            v["pass"]        # True / False / None when it could not judge
            v["source"]      # engine = learned, llm = a model was asked
            v["probability"] # calibrated from outcomes, not a stated number

    `pass` is None, never False, when the door has no evidence and no model: a
    judge that cannot judge must not report a verdict. Correct one with
    `judge_outcome` and the next output of that SHAPE is answered from evidence
    with no model call.
    """
    crits = [str(c) for c in criteria if str(c).strip()]
    if not crits:
        raise ValueError("criteria must not be empty")
    return _post(
        "/judge",
        {
            "output": str(output),
            "criteria": crits[:32],
            "domain": _domain(fork),
            "min_confidence": float(min_confidence),
        },
        timeout,
    )


def judge_outcome(
    decision_id: Optional[str] = None,
    verdict_was_right: Optional[bool] = None,
    *,
    output: Optional[str] = None,
    criterion: Optional[str] = None,
    should_pass: Optional[bool] = None,
    fork: str = "judge",
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Teach the judge: a verdict was right/wrong, or the truth outright."""
    if decision_id and verdict_was_right is not None:
        body: Dict[str, Any] = {
            "decision_id": str(decision_id),
            "verdict_was_right": bool(verdict_was_right),
        }
    elif criterion and should_pass is not None:
        body = {
            "output": str(output or ""),
            "criterion": str(criterion),
            "should_pass": bool(should_pass),
            "domain": _domain(fork),
        }
    else:
        raise ValueError(
            "pass decision_id + verdict_was_right, or criterion + should_pass (+ output)"
        )
    return _post("/judge/outcome", body, timeout)


def stats(timeout: float = 15.0) -> Dict[str, Any]:
    url = _url()
    req = urllib.request.Request(url + "/decide/stats")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx(url)) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DecideUnavailableError(f"decide door unreachable at {url}: {e}") from None


__all__ = [
    "Decision",
    "DecideUnavailableError",
    "decide",
    "decide_batch",
    "outcome",
    "teach",
    "judge",
    "judge_outcome",
    "stats",
]

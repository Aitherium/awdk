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
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

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
        return self.source == "engine"


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
    return _shape(_post("/decide", body, timeout))


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
    return [
        _shape(a) for a in _post("/decide/batch", {"items": payload}, timeout).get("answers", [])
    ]


def outcome(decision_id: str, reward: float, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Teach the door: reward in -1..1 for the decision you acted on."""
    return _post("/decide/outcome", {"decision_id": decision_id, "reward": float(reward)}, timeout)


def teach(
    fork: str, state: str, answer: Any, reward: float, *, timeout: float = 15.0
) -> Dict[str, Any]:
    """Teach without a prior decision: 'in this state, this answer earned this'."""
    return _post(
        "/decide/outcome",
        {"domain": _domain(fork), "state": state, "answer": answer, "reward": float(reward)},
        timeout,
    )


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

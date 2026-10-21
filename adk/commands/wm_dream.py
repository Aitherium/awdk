"""``adk wm dream`` -- see what a world model imagines next, from the shell.

Three imaginations run on an AitherOS fleet, each with its own viewer page:

  cns         AitherCNS rolls its fused latent forward and decodes it into fleet
              telemetry or routine steps          GET /dream, page /dream/view
  prospector  the Prospector's dynamics roll fleet telemetry K bins forward
                                                  GET /prospector/dream, page /prospector/view
  arc         the ARC world model decodes an imagined rollout of the board
                                                  GET /api/dream/ahead, page /

Every answer carries the honest yardstick next to the dream: the model's error vs
predicting "next = now" (copy-last). This command prints that verdict first, so an
agent (adk, awsh, Claude Code) can decide whether a dream is worth acting on.

Exit codes: 0 a dream was read · 1 the source answered but has nothing yet (no
bins, no frames) · 2 could not reach the source.
"""

from __future__ import annotations

import json
import os
import webbrowser
from typing import Any, Dict, Optional, Tuple

SOURCES: Dict[str, Dict[str, str]] = {
    "cns": {"env": "AITHER_CNS_URL", "base": "https://127.0.0.1:8194",
            "api": "/dream", "page": "/dream/view"},
    "prospector": {"env": "AITHER_CNS_URL", "base": "https://127.0.0.1:8194",
                   "api": "/prospector/dream", "page": "/prospector/view"},
    "arc": {"env": "ARC_PLAYGROUND_URL", "base": "http://127.0.0.1:8198",
            "api": "/api/dream/ahead", "page": "/"},
}


def _base(source: str) -> str:
    s = SOURCES[source]
    return (os.environ.get(s["env"]) or s["base"]).rstrip("/")


def _params(source: str, args: Any) -> Dict[str, Any]:
    steps = int(getattr(args, "steps", 6) or 6)
    if source == "cns":
        return {"modality": getattr(args, "modality", None) or "telemetry", "steps": steps}
    if source == "prospector":
        return {"rows": int(getattr(args, "rows", 16) or 16)}
    return {"action": int(getattr(args, "action", 1) or 1), "steps": steps}


def fetch(source: str, args: Any) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """(status, json, error). status 0 = transport failure."""
    import httpx

    from adk._tls import tls_verify

    url = _base(source) + SOURCES[source]["api"]
    try:
        with httpx.Client(verify=tls_verify(), timeout=60.0) as c:
            r = c.get(url, params=_params(source, args))
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, "" if r.status_code < 300 else (r.text or "")[:200]


def summarize(source: str, d: Dict[str, Any]) -> list:
    """Human lines: the verdict first, then what it imagines."""
    out = []
    if source == "arc":
        frames = d.get("frames") or []
        out.append(f"ARC world model: {len(frames)} imagined frame(s) for action "
                   f"{d.get('action')}")
        return out
    if source == "prospector":
        m, c = d.get("model_err"), d.get("copy_last_err")
    else:
        m, c = d.get("model_mse"), d.get("copy_last_mse")
    beats = d.get("beats_copy_last")
    if m is not None and c is not None:
        word = "BEATS" if beats else "does NOT beat"
        out.append(f"model error {m:.4g} vs next=now {c:.4g} -- {word} copy-last")
    if source == "prospector":
        out.append(f"{d.get('history_bins')} bins seen, {d.get('rollout_bins')} imagined "
                   f"({int(d.get('bin_seconds') or 0) // 60 or 1} min each), "
                   f"{d.get('features_total')} features")
        for x in (d.get("crossings") or [])[:3]:
            out.append(f"forecast: {x.get('key')} {x.get('current')} -> {x.get('predicted')} "
                       f"in ~{int(x.get('eta_s') or 0) // 60} min")
        for row in (d.get("rows") or [])[:8]:
            seen = (row.get("history") or [None])[-1]
            imag = (row.get("rollout") or [None])[-1]
            out.append(f"  {row.get('key'):<34} now {seen!s:>7}  imagined {imag!s:>7}")
    else:
        frames = d.get("frames") or []
        out.append(f"CNS {d.get('modality')}: {len(frames)} imagined step(s), "
                   f"width {len(frames[0]) if frames else 0}")
    return out


def cmd_wm_dream(args: Any) -> int:
    source = getattr(args, "source", None) or "cns"
    if source not in SOURCES:
        print(f"unknown source {source!r}; one of {', '.join(SOURCES)}")
        return 2
    page = _base(source) + SOURCES[source]["page"]
    status, body, err = fetch(source, args)
    if status == 0:
        print(f"could not reach {source} at {_base(source)}: {err}")
        return 2
    if status == 404 or status == 503:
        print(f"{source} has nothing to dream from yet ({status}): {err}")
        return 1
    if status >= 300 or body is None:
        print(f"{source} answered {status}: {err}")
        return 2
    if getattr(args, "json", False):
        print(json.dumps(body))
    else:
        for line in summarize(source, body):
            print(line)
        print(f"view: {page}")
    if getattr(args, "open", False):
        webbrowser.open(page)
    return 0

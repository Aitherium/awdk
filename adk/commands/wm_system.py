"""``adk wm system`` -- the world model of AitherOS itself (served by AitherCNS).

    adk wm system status              model, ingest, held-out eval, explorer coverage
    adk wm system surprise [OBJECT]   surprise now (one object, or the top list)
    adk wm system forecast            objects glowing warn/alert
    adk wm system whatif OBJECT       learned cascade if OBJECT goes down
    adk wm system episodes            recent exploration episodes
    adk wm system check "ACTION"      how-to-be-Aither: owner precedents this action matches
    adk wm system pulse               the anonymous public pulse

Endpoint: ``AITHER_CNS_URL`` (default ``https://127.0.0.1:8194``). Contracts:
``docs/system-world-model.md``. Exit 0 ok, 1 the model answered an error, 2 unreachable.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

DEFAULT_BASE = "https://127.0.0.1:8194"


def _base() -> str:
    return (os.environ.get("AITHER_CNS_URL") or DEFAULT_BASE).rstrip("/")


def request(path: str, body: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any, str]:
    """(status, json, error); status 0 = transport failure."""
    import httpx

    from adk._tls import tls_verify

    try:
        with httpx.Client(verify=tls_verify(), timeout=120.0) as c:
            if body is None:
                r = c.get(_base() + path, params=params)
            else:
                r = c.post(_base() + path, json=body)
    except Exception as e:  # noqa: BLE001 -- any transport failure is "unreachable"
        return 0, None, f"{type(e).__name__}: {e}"
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data, "" if r.status_code < 300 else (r.text or "")[:200]


def route(args: Any) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(path, body, params) for one subcommand."""
    sub = getattr(args, "system_command", None) or "status"
    obj = getattr(args, "object_id", None)
    if sub == "surprise":
        return "/wm/system/surprise", None, ({"object_id": obj} if obj else None)
    if sub == "forecast":
        return "/wm/system/forecast", None, {"glow_only": "true", "limit": 50}
    if sub == "whatif":
        return "/wm/system/whatif", {"object_id": obj, "status": "down"}, None
    if sub == "episodes":
        return "/wm/system/episodes", None, {"limit": 5}
    if sub == "check":
        return "/wm/aither/check", {"action": getattr(args, "action", "")}, None
    if sub == "pulse":
        return "/wm/system/pulse", None, None
    return "/wm/system/status", None, None


def cmd_wm_system(args: Any) -> int:
    path, body, params = route(args)
    code, data, err = request(path, body, params)
    if code == 0:
        print(f"could not reach the system world model at {_base()}: {err}")
        return 2
    if code >= 300:
        print(f"HTTP {code}: {err}")
        return 1
    if path == "/wm/system/status" and isinstance(data, dict) and not getattr(args, "json", False):
        m, e, ex = data.get("model") or {}, data.get("eval") or {}, data.get("explorer") or {}
        acc = (e.get("next_state") or {}).get("acc_changed_only") or {}
        fc = e.get("forecast_bad_next") or {}
        sp = e.get("surprise_precedes_incident") or {}
        print(f"system world model {m.get('version')}: {m.get('observations')} observations, "
              f"{m.get('objects')} objects")
        print(f"held-out: changed-row acc {acc.get('model')} (majority {acc.get('majority')}); "
              f"P(bad) AUROC {fc.get('auroc_model')} (persistence {fc.get('auroc_persistence')}); "
              f"surprise->incident AUROC {sp.get('auroc_surprise')} "
              f"(flap {sp.get('auroc_flap_rate_baseline')})")
        print(f"explorer: {ex.get('episodes')} episodes, coverage {ex.get('coverage')}")
        return 0
    print(json.dumps(data, indent=1, default=str))
    return 0

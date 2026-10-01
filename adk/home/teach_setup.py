"""``adk home teach setup`` -- a teacher's own agent, on their own computer, at no cost.

One command, in this order:

1. **Probe** the edge for ``/api/v1/classroom`` BEFORE any other network call. A
   routed endpoint answers 401/403 to an anonymous GET (or 200/422); a 404, a 5xx or
   no answer means the classroom is not reachable from here, and the setup says
   ``classroom: offline`` plainly instead of pretending.
2. ``init`` the Agent Home if there is none.
3. Pick the local **Bonsai** model preset (``models.PRESETS['bonsai']``) unless the
   home already runs a local Bonsai (``bonsai`` / ``bonsai2``) or ``keep_model``.
   ``keep_model`` keeps a LOCAL model only: a bring-your-own-key or remote model is
   refused (``HomeError``), because the classroom tools never run on one.
4. **Sign in** (device code) when the home holds no sign-in and the edge is online.
5. Record ``AITHER_HOME_TEACHER=1`` (and the classroom URL) in ``<home>/teacher.json``,
   which :func:`adk.home.serve.teacher_enabled` reads.
6. Print the ``adk home serve --install`` hint.

Nothing here downloads a model or starts a server: ``adk home model --local bonsai
--check`` and the Bonsai installer do that, and the hint names them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config as hc
from . import models

PROBE_PATH = "/api/v1/classroom/classes"
_PROBE_TIMEOUT = 10.0
#: Anonymous answers that prove the route exists behind auth.
_ROUTED = (200, 401, 403, 422)
LOCAL_BONSAI = ("bonsai", "bonsai2")


def probe_classroom(base_url: str, client: Optional[Any] = None) -> Dict[str, Any]:
    """Does the edge at ``base_url`` route ``/api/v1/classroom``? Never raises."""
    url = (base_url or "").rstrip("/") + PROBE_PATH
    try:
        if client is not None:
            resp = client.request("GET", url)
        else:
            import httpx

            from .teacher_tools import _verify

            with httpx.Client(timeout=_PROBE_TIMEOUT, verify=_verify()) as c:
                resp = c.request("GET", url)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return {"online": False, "url": url, "status": None,
                "why": f"unreachable ({type(exc).__name__})"}
    code = int(resp.status_code)
    if code in _ROUTED:
        return {"online": True, "url": url, "status": code, "why": "routed"}
    why = ("the edge does not route /api/v1/classroom" if code == 404
           else f"the edge answered HTTP {code}")
    return {"online": False, "url": url, "status": code, "why": why}


def write_teacher_state(url: str, root: Optional[Path] = None) -> Path:
    from .serve import TEACHER_FLAG_ENV, TEACHER_STATE

    path = (root or hc.home_dir()) / TEACHER_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({TEACHER_FLAG_ENV: "1", "url": url.rstrip("/")}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def setup(url: str = "", *, signin: bool = True, keep_model: bool = False,
          root: Optional[Path] = None,
          probe: Callable[[str], Dict[str, Any]] = probe_classroom,
          do_signin: Optional[Callable[[], int]] = None,
          signed_in: Optional[Callable[[], bool]] = None,
          out: Callable[[str], None] = print) -> Dict[str, Any]:
    """Run the setup; returns what it did (the CLI prints it). See the module doc."""
    from .serve import teacher_url

    base = (url or teacher_url(root)).rstrip("/")
    result: Dict[str, Any] = {"url": base}

    # 1. the probe comes before ANY other network call
    reach = probe(base)
    result["classroom"] = "online" if reach.get("online") else "offline"
    result["probe"] = reach
    if reach.get("online"):
        out(f"classroom: online ({base})")
    else:
        out(f"classroom: offline -- {reach.get('why')} ({reach.get('url')}). Setup "
            "continues on this computer; the classroom tools answer 'unreachable' "
            "until it is online.")

    # 2. init
    if not hc.is_initialized(root):
        hc.init_home(name="teacher-agent", root=root)
        result["init"] = "created"
    else:
        result["init"] = "existing"

    # 3. the local Bonsai preset
    cfg = hc.load_config(root)
    if keep_model and not models.is_local(cfg.model):
        # Student records never go to a bring-your-own-key or remote model: refuse
        # before the teacher flag is written, so nothing is half on.
        from .serve import TEACHER_NEEDS_LOCAL

        raise hc.HomeError(f"--keep-model refused ({cfg.model.provider} is not a model "
                           f"on this computer): {TEACHER_NEEDS_LOCAL}")
    if keep_model or (models.is_local(cfg.model) and cfg.model.provider in LOCAL_BONSAI):
        result["model"] = f"kept {cfg.model.provider}"
    else:
        cfg.model = models.choose_model("bonsai")
        hc.save_config(cfg, root)
        result["model"] = "bonsai"
        out("model: local Bonsai (nothing leaves this computer). "
            + models.bonsai_install_hint())

    # 4. sign in when needed and reachable
    if signed_in is None:
        from .connector_tools import home_signed_in as signed_in
    if signed_in():
        result["signin"] = "existing"
    elif not signin:
        result["signin"] = "skipped"
    elif not reach.get("online"):
        result["signin"] = "skipped (offline)"
    elif do_signin is None:
        result["signin"] = "needed"
    else:
        result["signin"] = "done" if do_signin() == 0 else "failed"

    # 5. the flag
    result["state"] = str(write_teacher_state(base, root))
    out("teacher tools: on (AITHER_HOME_TEACHER=1 in " + result["state"] + ")")

    # 6. the hint
    out("Next: adk home model --local bonsai --check, then "
        "adk home serve --install (your agent starts at logon and answers only you).")
    return result
